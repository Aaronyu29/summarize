import copy
import json
import unittest
from rhino_invest.common import stable_id
from rhino_invest.store import Store
from rhino_invest.reuse import completed_evidence_plan
from rhino_invest.evidence_transport import lossless_graph, unpack_graph, messages
from rhino_invest.config import Config
from rhino_invest.model import ModelGateway
from tests_invest.test_model import CONFIG, NOW
from tests_invest import test_research_critique as critiques
from tests_invest import test_research_evidence_contract as fixtures
from rhino_invest.research import ingest_archives
from rhino_invest.research import _run
from rhino_invest.model import ModelOutputInvalid


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:');self.addCleanup(self.s.close)
        self.facts={'financial_report':[{'eps':12,'period':'2026-Q3'}],'fetched_at':'old'}
        self.s.put('fundamental_evidence','facts',{'data':self.facts},immutable=True)
        self.p={'id':'plan','symbol':'ABC','revision':5,'status':'needs_review',
                'research_snapshot_id':'video','fundamental_evidence_id':'facts','review_issues':['No price support']}
        self.s.put('plan_operation','op',{'plan':self.p},immutable=True)

    def test_rejected_result_reused_across_ticks_days_and_internal_repairs(self):
        for t in [NOW,'2026-10-29T15:00:00+00:00']:
            result=completed_evidence_plan(self.s,{'id':'video'},'ABC',dict(self.facts,fetched_at=t),
                {'kind':'system-plan-remediation-v1','event_id':t,'revision':99},t)
            self.assertEqual(result,self.p)
            self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len(self.s.items('research_evidence_reuse')),1)
        self.assertEqual(self.s.items('model_call'),[])

    def test_new_financial_fact_video_or_external_risk_cannot_reuse(self):
        for source,facts,context in [({'id':'video2'},self.facts,None),
                ({'id':'video'},{'financial_report':[{'eps':13,'period':'2026-Q3'}]},None),
                ({'id':'video'},self.facts,{'kind':'risk','new_evidence':{'issuer_filing':'new'}})]:
            self.assertIsNone(completed_evidence_plan(self.s,source,'ABC',facts,context,NOW))

    def test_never_overwrites_newer_different_evidence_plan(self):
        self.s.put('plan','plan',dict(self.p,revision=7,research_snapshot_id='new-video'))
        self.assertIsNone(completed_evidence_plan(self.s,{'id':'video'},'ABC',self.facts,None,NOW))


class TransportTests(unittest.TestCase):
    def test_invalid_research_receipt_never_buys_format_regeneration(self):
        for purpose in ('compile_plan','review_plan'):
            with self.subTest(purpose=purpose):
                s=Store(':memory:');self.addCleanup(s.close);calls=[]
                def transport(body):
                    calls.append(body)
                    return {'choices':[{'finish_reason':'stop','message':{'content':'{bad-json'}}]}
                model=ModelGateway(s,dict(CONFIG,reuse_evidence_cycles=True),transport)
                for day in (NOW,'2026-10-29T15:00:00+00:00'):
                    with self.assertRaises(ModelOutputInvalid):
                        _run(model,purpose,{'symbol':'ABC'},now=day,store=s)
                self.assertEqual(len(calls),1)
                self.assertEqual(len(s.items('model_receipt')),1)

    def test_graph_is_lossless_and_source_marker_cannot_spoof_reference(self):
        text='primary audited passage '*1000
        original={'a':text,'b':text,'raw':{'$research_ref':'untrusted'},'nested':[{'v':text}]}
        graph=lossless_graph(original)
        self.assertEqual(unpack_graph(graph),original)
        self.assertLess(len(json.dumps(graph)),len(json.dumps(original))*.55)

    def test_financial_rows_preserve_all_columns_and_order(self):
        rows=[{'long_financial_metric_name':i,'period_end':'2026-Q3',
               'source_filing_reference':'issuer','unit':'USD','unknown':None} for i in range(100)]
        graph=lossless_graph({'financial_report':rows,'literal':{'$table':{'columns':['fake'],'rows':[[1]]}}})
        self.assertEqual(unpack_graph(graph),{'financial_report':rows,'literal':{'$table':{'columns':['fake'],'rows':[[1]]}}})
        self.assertLess(len(json.dumps(graph)),len(json.dumps(rows))*.5)

    def test_generation_and_review_share_exact_primary_prefix(self):
        f={'financial_report':{'text':'issuer original '*1000}}
        a=messages('system','compile original','compile_plan',{'fundamentals':f,'symbol':'ABC'})
        b=messages('system','independently review','review_plan',{'fundamentals':f,'plan':{'entry':1},'review_context':{'prior_fundamentals':f}})
        self.assertEqual(a[:2],b[:2])
        self.assertIn('ONLY the final user message',a[0]['content'])
        self.assertEqual(json.loads(b[-1]['content'][0]['text'])['trusted_task'],'independently review')
        self.assertIn('$dossier_pointer',b[-1]['content'][0]['text'])
        self.assertNotIn('issuer original',b[-1]['content'][0]['text'])

    def test_gateway_preserves_completed_result_cache_and_records_size(self):
        s=Store(':memory:');self.addCleanup(s.close);calls=[]
        def transport(body):
            calls.append(body)
            return {'choices':[{'finish_reason':'stop','message':{'content':'{"plan":{}}'}}]}
        g=ModelGateway(s,dict(CONFIG,stable_evidence_transport=True),transport)
        for i in range(4):g.run('compile_plan',{'fundamentals':{'text':'source '*500}},now=NOW)
        self.assertEqual(len(calls),1)
        record=s.items('model_call')[0][1]
        self.assertEqual(record['transport'],'stable-lossless-evidence-v1')
        self.assertGreater(record['request_text_chars'],0)

    def test_loaded_production_config_enables_reuse(self):
        c=Config.load(environ={})
        self.assertTrue(c.model['reuse_evidence_cycles'])
        self.assertTrue(c.model['stable_evidence_transport'])


class CompletedCycleIntegrationTests(unittest.TestCase):
    setUp=fixtures.EvidenceContractTests.setUp
    facts=critiques.CritiqueTests.facts

    def test_verified_fund_missing_valuation_gets_one_reviewed_zero_allocation_abstention(self):
        class AbstentionModel(fixtures.IndependentModel):
            config={'enabled':True,'reuse_evidence_cycles':True,'bounded_feedback_since':'1970-01-01T00:00:00Z'}
            def run(self,purpose,payload,**kwargs):
                result=super().run(purpose,payload,**kwargs)
                if purpose=='review_plan':result['no_buy_reviewed']=True
                return result
        data=self.facts();data['identity'][0]['name_en']='Official Test ETF'
        data['primary_financials']['instrument']['stock_eps_valuation_applicable']=False
        model=AbstentionModel()
        result=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':data},now=NOW)
        self.assertEqual(result['errors'],[])
        p=result['plans'][0]
        self.assertEqual(p['research_assessment']['status'],'approved_no_buy',p.get('review_issues'))
        self.assertEqual(p['entry']['amount_usd'],'0')
        self.assertIsNone(p['horizon_months'])
        self.assertEqual(model.calls.count('compile_plan'),0)
        self.assertEqual(model.calls.count('review_plan'),1)
        ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':data},now='2026-10-29T15:00:00Z')
        self.assertEqual(model.calls.count('review_plan'),1)
        from rhino_invest.common import validate_plan
        self.assertIn('no-buy research cannot authorize execution',validate_plan(p))

    def test_current_source_feedback_is_exactly_one_pair_and_never_resets_for_ticks(self):
        model=critiques.CritiqueModel(always_reject=True)
        model.config={'enabled':True,'reuse_evidence_cycles':True,'bounded_feedback_since':'1970-01-01T00:00:00Z'}
        facts=self.facts()
        initial=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':facts},now=NOW)
        self.assertEqual(initial['errors'],[])
        self.assertEqual(model.calls.count('compile_plan'),1)
        for _ in range(3):
            result=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':facts},now='2026-10-29T15:00:00+00:00')
            self.assertEqual(result['errors'],[])
        self.assertEqual(model.calls.count('compile_plan'),2)
        self.assertEqual(model.calls.count('review_plan'),2)
        self.assertEqual(len(self.store.items('research_critique_attempt')),1)
        self.assertEqual(len(self.store.items('research_critique_result')),1)
        changed=copy.deepcopy(facts);changed['financial_report']={'new_reported_eps':'5'}
        for _ in range(3):
            result=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':changed},now='2026-10-30T15:00:00Z')
            self.assertEqual(result['errors'],[])
        self.assertEqual(model.calls.count('compile_plan'),3)
        self.assertEqual(model.calls.count('review_plan'),3)
        self.assertEqual(len(self.store.items('research_critique_attempt')),1)

    def test_rejected_real_workflow_does_not_start_critique_on_future_ticks(self):
        model=critiques.CritiqueModel(always_reject=True)
        model.config={'enabled':True,'reuse_evidence_cycles':True}
        facts=self.facts()
        first=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':facts},now=NOW)
        self.assertEqual(first['errors'],[])
        count=len(model.calls)
        self.assertEqual(first['plans'][0]['status'],'needs_review')
        for i in range(3):
            result=ingest_archives(self.store,self.db,model=model,fundamentals={'NVDA':facts},now='2026-10-29T15:00:00+00:00')
            self.assertEqual(result['errors'],[])
        self.assertEqual(len(model.calls),count)
