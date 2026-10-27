"""Source JSON recovery is one paid child, followed by the original evidence gate."""
import copy
import json
import unittest

from rhino_invest.common import stable_id
from rhino_invest.model import ModelGateway, ModelBudgetExceeded, ModelOutputInvalid, UnknownModelOutcome
from rhino_invest.research import _run, _verify_levels, EXTRACT, VERIFY
from rhino_invest.store import Store

NOW='2026-10-27T03:52:00+00:00'
CONFIG={'enabled':True,'model':'offline-fixture','base_url':'https://model.invalid/v1',
        'max_cost_usd_per_call':'0.1','max_cost_usd_per_day':'1','max_calls_per_day':10}
LEVEL={'symbol':'TSM','kind':'technical_support','value':100,'low':None,'high':None,
       'currency':'USD','price_unit':'per_adr','source_date':'2026-10-27','adjustment':'unadjusted',
       'source_horizon':'historical','timestamp_seconds':10,'evidence_segment_ids':[0],
       'frame_seconds':10,'conditions':'creator conditional support','uncertainty':None}
OUTPUTS={'extract_levels':{'levels':[LEVEL]},'verify_levels':{'checks':[{'index':0,'verified':False,'issues':['Original evidence is ambiguous']}]}}


def response(value,finish='stop'):
    return {'choices':[{'finish_reason':finish,'message':{'content':value}}]}


class SourceRecoveryTests(unittest.TestCase):
    def model(self,store,replies,calls,config=None,clock=NOW):
        def transport(body):
            calls.append(copy.deepcopy(body))
            value=replies.pop(0)
            if isinstance(value,Exception):raise value
            return value
        return ModelGateway(store,config or CONFIG,transport,clock=lambda:clock)

    def invoke(self,model,purpose):
        return model.run(purpose,{'source':{'original':'complete source'},'original_levels':[LEVEL]},
            now=NOW,instruction='Process the original evidence.',call_scope='source-batch-0')

    def test_completed_malformed_replays_one_cached_child_for_both_source_stages(self):
        for purpose,output in OUTPUTS.items():
            with self.subTest(purpose=purpose),Store(':memory:') as store:
                calls=[];raw=response('{"broken": 1 "missing_comma":2}')
                legacy=self.model(store,[raw],calls)
                with self.assertRaises(ModelOutputInvalid):
                    legacy._run_once(purpose,{'source':{'original':'complete source'},'original_levels':[LEVEL]},
                        now=NOW,instruction='Process the original evidence.',call_scope='source-batch-0')
                self.assertEqual(self.invoke(self.model(store,[response(json.dumps(output))],calls),purpose),output)
                self.assertEqual(self.invoke(self.model(store,[],calls,clock='2026-10-28T03:00:00Z'),purpose),output)
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0]['messages'][1],calls[1]['messages'][1])
                self.assertIn('SOURCE_JSON_RECOVERY_ATTEMPT=1',calls[1]['messages'][0]['content'])
                audit=store.items('model_recovery')[0][1]
                parent=store.get('model_receipt',audit['parent_call_id'])
                self.assertEqual(parent['response'],raw)
                self.assertEqual(audit['parent_receipt_hash'],stable_id(parent))
                self.assertEqual(len(store.items('model_response')),1)

    def test_twice_malformed_is_terminal_after_restart_and_day_change(self):
        for purpose in OUTPUTS:
            with self.subTest(purpose=purpose),Store(':memory:') as store:
                calls=[];model=self.model(store,[response('{bad'),response('{still bad')],calls)
                with self.assertRaises(ModelOutputInvalid):self.invoke(model,purpose)
                with self.assertRaises(ModelOutputInvalid):self.invoke(self.model(store,[],calls,clock='2026-10-28T00:00:00Z'),purpose)
                self.assertEqual(len(calls),2)

    def test_unknown_parent_and_unknown_child_keep_durable_fences(self):
        for purpose in OUTPUTS:
            for replies,count in [([TimeoutError()],1),([response('{bad'),TimeoutError()],2)]:
                with self.subTest(purpose=purpose,count=count),Store(':memory:') as store:
                    calls=[]
                    with self.assertRaises(UnknownModelOutcome):self.invoke(self.model(store,replies,calls),purpose)
                    with self.assertRaises(UnknownModelOutcome):self.invoke(self.model(store,[],calls),purpose)
                    changed=self.model(store,[],calls,dict(CONFIG,configuration_revision=9))
                    with self.assertRaises(UnknownModelOutcome):self.invoke(changed,purpose)
                    self.assertEqual(len(calls),count)

    def test_both_budget_limits_still_cover_recovery(self):
        for purpose in OUTPUTS:
            for limit in ({'max_calls_per_day':1},{'max_cost_usd_per_day':'0.1'}):
                with self.subTest(purpose=purpose,limit=limit),Store(':memory:') as store:
                    calls=[]
                    with self.assertRaises(ModelBudgetExceeded):
                        self.invoke(self.model(store,[response('{bad')],calls,dict(CONFIG,**limit)),purpose)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(self.invoke(self.model(store,[response(json.dumps(OUTPUTS[purpose]))],calls),purpose),OUTPUTS[purpose])
                    self.assertEqual(len(calls),2)

    def test_known_non_syntax_defects_never_buy_format_retry(self):
        for purpose in OUTPUTS:
            for provider in [response('{}','length'),response('[]'),response(None),response('null')]:
                with self.subTest(purpose=purpose,provider=provider),Store(':memory:') as store:
                    calls=[]
                    with self.assertRaises(ModelOutputInvalid):self.invoke(self.model(store,[provider],calls),purpose)
                    self.assertEqual(len(calls),1)
                    self.assertEqual(store.items('model_recovery'),[])

    def test_child_must_match_source_schema_and_never_force_verification(self):
        cases=[('extract_levels',{'levels':[{'value':100}]}),('verify_levels',{'checks':[{'index':0,'verified':'yes','issues':[]}]})]
        for purpose,output in cases:
            with self.subTest(purpose=purpose),Store(':memory:') as store:
                calls=[]
                with self.assertRaises(ModelOutputInvalid):
                    self.invoke(self.model(store,[response('{bad'),response(json.dumps(output))],calls),purpose)
                with self.assertRaises(ModelOutputInvalid):self.invoke(self.model(store,[],calls),purpose)
                self.assertEqual(len(calls),2)

    def test_restored_extraction_still_runs_independent_original_evidence_verification(self):
        with Store(':memory:') as store:
            calls=[];model=self.model(store,[response('{bad'),response(json.dumps(OUTPUTS['extract_levels'])),response(json.dumps(OUTPUTS['verify_levels']))],calls)
            snapshot={'id':'original-source','transcript':{'segments':[{'id':0,'start':0,'end':20,'text':'original conditional support'}]},'frames':[{'timestamp_seconds':10}]}
            snapshot.update(video_id='abcdefghijk',revision=1,title='original source',metadata={},
                            analysis_sha256='sealed-analysis',manifest_sha256='sealed-manifest')
            levels=_run(model,'extract_levels',{'instruction':EXTRACT,'source':snapshot},now=NOW)
            checked=_run(model,'verify_levels',{'instruction':VERIFY,'source':snapshot,'levels':levels},now=NOW)
            result=_verify_levels(snapshot,levels,checked)
            self.assertEqual(len(calls),3)
            self.assertFalse(result[0]['verified'])
            self.assertIn('Original evidence is ambiguous',result[0]['issues'])

    def test_child_rejects_duplicate_keys_instead_of_discarding_original_values(self):
        with Store(':memory:') as store:
            calls=[]
            with self.assertRaises(ModelOutputInvalid):
                self.invoke(self.model(store,[response('{bad'),response('{"levels":[{}],"levels":[]}')],calls),'extract_levels')
            self.assertEqual(len(calls),2)


if __name__=='__main__':unittest.main()
