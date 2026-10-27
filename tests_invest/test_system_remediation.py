import copy
import unittest
from unittest.mock import patch

from rhino_invest.common import stable_id
from rhino_invest.config import Config
from rhino_invest.model import ModelBudgetExceeded
from rhino_invest.remediation import remediate_plans, adjudicate_research, accepted_fallback, resolution_errors, economic_consistency_errors
from rhino_invest.research import ingest_archives
from tests_invest import test_research_evidence_contract as evidence_tests
from tests_invest.test_research import NOW


class RepairModel(evidence_tests.IndependentModel):
    def run(self,purpose,payload,**kwargs):
        result=super().run(purpose,payload,**kwargs)
        if purpose=='compile_plan' and (payload.get('review_context') or {}).get('kind')=='system-plan-remediation-v1':
            prior=payload['review_context']['prior_plan']
            result['plan']['condition_resolution']=[{'previous_condition':text,'disposition':'retained',
                'reason':'The same exact initial price rule remains in this fixture.','evidence_refs':[]} for text in prior['entry']['conditions']]
        if purpose=='review_plan':result['condition_resolution_reviewed']=True
        return result


class RemediationTests(unittest.TestCase):
    setUp=evidence_tests.EvidenceContractTests.setUp
    primary=evidence_tests.EvidenceContractTests.primary

    def seed(self):
        data,_=self.primary();data['company_profile']={'company_name':'Fixture issuer'}
        old=ingest_archives(self.store,self.db,model=evidence_tests.IndependentModel(reject=True),fundamentals={'NVDA':data},now=NOW)['plans'][0]
        return data,old

    def test_system_repairs_and_independently_reviews_without_asserting_cash_verified(self):
        data,old=self.seed();model=RepairModel()
        original=self.store.get('research_plan_review',old['review_evidence_id'])
        result=remediate_plans(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(result['errors'],[])
        new=self.store.get('plan',old['id']);self.assertEqual(new['status'],'active',new.get('review_issues'))
        self.assertEqual(new['entry']['amount_usd'],'1000')
        self.assertEqual(model.calls,['compile_plan','review_plan'])
        self.assertEqual(self.store.get('research_plan_review',old['review_evidence_id']),original)
        review_payload=next(p for purpose,p in model.payloads if purpose=='review_plan')
        self.assertIn('condition_resolution',review_payload['plan'])
        self.assertFalse(review_payload['policy']['cash_verified'])
        remediate_plans(self.store,model,{'NVDA':data},'2026-10-24T00:00:00Z')
        self.assertEqual(len(model.calls),2)
        original_get=self.store.get
        def without_legacy_operation(kind,key):
            return None if kind=='plan_operation' else original_get(kind,key)
        with patch.object(self.store,'get',side_effect=without_legacy_operation):
            replay=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':data},now=NOW)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(replay['plans'][0]['status'],'active')

    def test_rejection_is_two_rounds_and_does_not_reset_on_restarts_or_new_revision(self):
        data,old=self.seed();model=RepairModel(reject=True)
        remediate_plans(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(model.calls,['compile_plan','review_plan']*2)
        for _ in range(3):remediate_plans(self.store,model,{'NVDA':data},'2026-10-24T00:00:00Z')
        self.assertEqual(len(model.calls),4)
        jobs=self.store.items('research_system_repair');self.assertEqual(len(jobs),1)
        self.assertTrue(jobs[0][1]['terminal']);self.assertEqual(jobs[0][1]['round'],2)
        inputs=[v for k,v in self.store.items('research_system_repair_input')]
        self.assertEqual(len(inputs),2)
        self.assertEqual(inputs[0]['prior_plan'],inputs[1]['prior_plan'])
        self.assertNotEqual(inputs[0]['latest_failed_plan']['revision'],inputs[1]['latest_failed_plan']['revision'])

    def test_unknown_request_blocks_new_repair_identity_and_never_calls_model(self):
        data,_=self.seed();model=RepairModel()
        self.store.put('model_call','unknown',{'timestamp':NOW,'purpose':'compile_plan'},immutable=True)
        result=remediate_plans(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(model.calls,[]);self.assertEqual(result['errors'][0]['error'],'UnknownModelOutcome')

    def test_budget_deferral_resumes_exact_frozen_context_without_reset(self):
        data,old=self.seed();model=RepairModel();calls=[]
        def fake(store,source,levels,model,fundamentals,now,context):
            calls.append((copy.deepcopy(context),now))
            if len(calls)==1:raise ModelBudgetExceeded('budget')
            return [dict(old,status='active',revision=99)]
        with patch('rhino_invest.remediation.compile_plan',side_effect=fake):
            first=remediate_plans(self.store,model,{'NVDA':data},NOW)
            self.assertEqual(first['deferred'][0]['error'],'ModelBudgetExceeded')
            remediate_plans(self.store,model,{'NVDA':data},'2026-10-24T00:00:00Z')
        self.assertEqual(calls[0],calls[1])

    def test_verified_risk_blocks_repair_without_paid_request(self):
        data,_=self.seed();model=RepairModel()
        with patch('rhino_invest.remediation._unresolved_thesis_defer',return_value={'reason':'risk'}):
            result=remediate_plans(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(model.calls,[]);self.assertTrue(result['deferred'])

    def test_original_conditions_cannot_be_silently_deleted_or_renamed_cash_gates(self):
        context={'prior_plan':{'entry':{'conditions':['Await next issuer financial release']}}}
        p={'entry':{'conditions':['Price within band']},'condition_resolution':[]}
        self.assertTrue(resolution_errors(p,{'condition_resolution_reviewed':True},context,{'source'}))
        p['condition_resolution']=[{'previous_condition':'Await next issuer financial release','disposition':'execution_gate','reason':'price only'}]
        self.assertTrue(resolution_errors(p,{'condition_resolution_reviewed':True},context,{'source'}))
        p['condition_resolution'][0].update(disposition='satisfied_by_evidence',evidence_refs=['invented'])
        self.assertTrue(resolution_errors(p,{'condition_resolution_reviewed':True},context,{'source'}))
        p['condition_resolution'][0]['evidence_refs']=['source']
        self.assertTrue(resolution_errors(p,{},context,{'source'}))

    def test_downside_take_profit_and_stop_above_initial_entry_are_deterministically_rejected(self):
        p={'entry':{'low':'90','high':'100'},'stop':{'price':'110'},'take_profit':[{'price':'70'}]}
        self.assertEqual(len(economic_consistency_errors(p)),2)
        p.update(stop={'price':'80'},take_profit=[{'price':'120'}])
        self.assertEqual(economic_consistency_errors(p),[])

    def test_runtime_remediation_requires_explicit_operator_policy(self):
        self.assertFalse(Config().system_remediation_enabled)
        self.assertTrue(Config.load(environ={'INVEST_SYSTEM_REMEDIATION_ENABLED':'true'}).system_remediation_enabled)
        with self.assertRaises(ValueError):Config(system_remediation_enabled='maybe')

    def conditional_baseline(self):
        from rhino_invest.research import seal_plan_review, _persist_plan
        data,_=self.primary();data['company_profile']={'company_name':'Fixture issuer'}
        original=ingest_archives(self.store,self.db,model=evidence_tests.IndependentModel(),fundamentals={'NVDA':data},now=NOW)['plans'][0]
        baseline=copy.deepcopy(original);baseline['revision']+=1
        baseline['status']='needs_review';baseline['entry'].update(condition_mode='price_and_fundamental',conditions=['Await next issuer report'],execution_conditions=[])
        baseline['conditions_parsed']=False;baseline['research_assessment']['status']='approved_conditional'
        old_review=self.store.get('research_plan_review',original['review_evidence_id'])['review']
        seal_plan_review(self.store,baseline,old_review)
        _persist_plan(self.store,baseline)
        return data,baseline

    def test_failed_replacement_keeps_accepted_baseline_and_review_only_phase_never_buys_draft(self):
        data,baseline=self.conditional_baseline();model=RepairModel(reject=True)
        remediate_plans(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(self.store.get('plan',baseline['id']),baseline)
        revisions=[p['revision'] for k,p in self.store.items('plan_revision') if p['id']==baseline['id']]
        self.assertEqual(len(set(revisions)),len(revisions));self.assertGreater(max(revisions),baseline['revision'])
        before=len(model.calls)
        result=adjudicate_research(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(result['processed'],1)
        self.assertEqual(model.calls[before:],['review_plan'])
        adjudicate_research(self.store,model,{'NVDA':data},NOW)
        self.assertEqual(len(model.calls),before+1)
        self.assertEqual(self.store.get('plan',baseline['id']),baseline)

    def test_legacy_rejected_publication_restores_baseline_before_budget_deferral(self):
        data,baseline=self.conditional_baseline()
        remediate_plans(self.store,RepairModel(reject=True),{'NVDA':data},NOW)
        job=self.store.items('research_system_repair')[0][1]
        failed=self.store.get('plan_revision',stable_id(baseline['id'],job['plan_revision']))
        self.store.put('plan',baseline['id'],failed)
        with patch('rhino_invest.remediation.compile_plan',side_effect=ModelBudgetExceeded('budget')):
            result=adjudicate_research(self.store,RepairModel(),{'NVDA':data},NOW)
        self.assertTrue(result['deferred'])
        self.assertEqual(self.store.get('plan',baseline['id']),baseline)
        self.store.put('plan',baseline['id'],failed)
        self.store.put('research_system_repair',job['id'],dict(job,terminal=False,status='budget_deferred'))
        with patch('rhino_invest.remediation.compile_plan') as call:
            adjudicate_research(self.store,RepairModel(),{'NVDA':data},NOW)
        call.assert_not_called()
        self.assertEqual(self.store.get('plan',baseline['id']),baseline)

    def test_fallback_never_restores_over_changed_facts_or_verified_risk(self):
        data,baseline=self.conditional_baseline();context={'prior_plan':baseline}
        self.assertEqual(accepted_fallback(self.store,context,baseline),baseline)
        self.assertIsNone(accepted_fallback(self.store,context,dict(baseline,fundamental_evidence_id='changed')))
        self.store.put('risk_evidence','risk',{'verified':True,'plan_id':baseline['id']})
        self.assertIsNone(accepted_fallback(self.store,context,baseline))

    def test_budget_work_order_follows_configured_watchlist(self):
        from rhino_invest.store import Store
        with Store(':memory:') as store:
            store.put('research_evidence','source',{'id':'source'})
            for symbol in ('CRCL','GOOG','TSM'):
                store.put('plan',symbol,{'id':symbol,'symbol':symbol,'status':'needs_review','revision':1,'research_snapshot_id':'source'})
            calls=[]
            def compile_fixture(store,source,levels,model,fundamentals,now,context):
                symbol=next(iter(fundamentals));calls.append(symbol)
                return [{'symbol':symbol,'id':symbol,'revision':2,'status':'active'}]
            with patch('rhino_invest.remediation._unresolved_thesis_defer',return_value=None), \
                 patch('rhino_invest.remediation._research_fundamental',side_effect=lambda store,symbol,data:({'symbol':symbol},None)), \
                 patch('rhino_invest.remediation.compile_plan',side_effect=compile_fixture):
                remediate_plans(store,None,{},NOW,('TSM','GOOG','CRCL'))
            self.assertEqual(calls,['TSM','GOOG','CRCL'])

    def test_economic_replacement_is_bounded_and_preserves_original_conditions(self):
        data,old=self.seed();contexts=[]
        def invalid(store,source,levels,model,fundamentals,now,context):
            contexts.append(copy.deepcopy(context))
            revised=copy.deepcopy(old)
            revised.update(revision=len(contexts)+10,entry={'low':'100','high':'110','conditions':['bad replacement']},
                           take_profit=[{'price':'90'}],stop={'price':'80'})
            store.put('plan',old['id'],revised)
            store.put('plan_revision',stable_id(old['id'],revised['revision']),revised)
            return [revised]
        with patch('rhino_invest.remediation.compile_plan',side_effect=invalid):
            remediate_plans(self.store,None,{'NVDA':data},NOW)
            self.store.put('model_call','unresolved-parent',{'timestamp':NOW},immutable=True)
            paused=remediate_plans(self.store,None,{'NVDA':data},NOW)
            self.assertEqual(len(contexts),2)
            self.assertEqual(paused['errors'][0]['error'],'UnknownModelOutcome')
            self.store.put('model_receipt','unresolved-parent',{'completed':True},immutable=True)
            for _ in range(4):remediate_plans(self.store,None,{'NVDA':data},NOW)
        self.assertEqual(len(contexts),4)  # Two original + two numeric-policy proposals.
        self.assertEqual(contexts[2]['prior_plan'],contexts[0]['prior_plan'])
        self.assertEqual(contexts[2]['repair_policy'],'system-economic-repair-v1')
        self.assertTrue(contexts[2]['calculation_feedback']['economic_errors'])
        self.assertEqual(len(self.store.items('research_system_repair')),2)

    def test_operating_bridge_computes_one_growth_tax_and_dilution_chain(self):
        from rhino_invest.research import _calculate_target_prices,_operating_eps
        bridge={'operating_income':'100','annual_growth':'.1','years':2,'tax_rate':'.2',
                'after_tax_nonoperating_cost':'6.8','diluted_shares':'9'}
        eps,details=_operating_eps(bridge)
        self.assertEqual(eps,10)
        self.assertEqual(details['after_tax_net_proxy'],'90.000')
        target={'method':'eps_multiple','price':'90','assumptions':{'eps':'999','multiple':20,'operating_eps_bridge':bridge}}
        p,book=_calculate_target_prices({'targets':[target],'entry':{'low':70,'high':80},'stop':{'price':60}})
        self.assertEqual(p['targets'][0]['price'],'200.00')
        self.assertEqual(p['targets'][0]['assumptions']['eps'],'10.000')
        self.assertEqual(p['entry'],{'low':70,'high':80})
        self.assertEqual(p['stop'],{'price':60})
        self.assertFalse(book['inputs_selected_by_calculator'])
        target['assumptions']['eps_bridge']={'anchor_eps':10,'annual_growth':'.1','years':2}
        _,book=_calculate_target_prices({'targets':[target]})
        self.assertEqual(book['targets'],[])  # Never silently apply both growth bridges.
        for bad in ({'diluted_shares':0},{'tax_rate':1},{'after_tax_nonoperating_cost':1000}):
            with self.assertRaises(ValueError):_operating_eps(dict(bridge,**bad))

    def test_condition_ids_expand_only_explicit_originals_before_independent_review(self):
        from rhino_invest.remediation import condition_catalog,expand_condition_ids
        context={'prior_plan':{'entry':{'conditions':['Await next issuer financial release','Require a fresh official quote']}}}
        catalog=condition_catalog(context)
        proposal={'entry':{'low':90,'high':100,'conditions':['Price between 90 and 100'],
                          'retained_condition_ids':[catalog[0]['condition_id']]},
                  'condition_resolution':[{'condition_id':catalog[0]['condition_id'],'disposition':'retained','reason':'Future report remains required'},
                    {'condition_id':catalog[1]['condition_id'],'disposition':'execution_gate','reason':'Engine quote check'}]}
        before=copy.deepcopy(proposal)
        expanded=expand_condition_ids(proposal,context)
        self.assertEqual(proposal,before)
        self.assertIn('Await next issuer financial release',expanded['entry']['conditions'])
        self.assertEqual(expanded['entry']['low'],90)
        self.assertEqual(resolution_errors(expanded,{'condition_resolution_reviewed':True},context,set()),[])
        self.assertTrue(resolution_errors(expanded,{},context,set()))
        for bad in ('unknown','duplicate','missing_retained','conflicting_text'):
            changed=copy.deepcopy(proposal)
            if bad=='unknown':changed['condition_resolution'][0]['condition_id']='invented'
            elif bad=='duplicate':changed['condition_resolution'][1]=changed['condition_resolution'][0]
            elif bad=='missing_retained':changed['entry']['retained_condition_ids']=[]
            else:changed['condition_resolution'][0]['previous_condition']='A different fundamental gate'
            with self.assertRaises(ValueError):expand_condition_ids(changed,context)


if __name__=='__main__':unittest.main()
