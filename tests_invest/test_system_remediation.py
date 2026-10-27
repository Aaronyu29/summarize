import copy
import unittest
from unittest.mock import patch

from rhino_invest.common import stable_id
from rhino_invest.config import Config
from rhino_invest.model import ModelBudgetExceeded
from rhino_invest.remediation import remediate_plans, resolution_errors, economic_consistency_errors
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


if __name__=='__main__':unittest.main()
