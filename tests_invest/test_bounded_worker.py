import json
import threading
import unittest
from unittest.mock import patch
from rhino_invest.store import Store
from rhino_invest.research import ingest_archives
from tests_invest import test_research as fixtures
from rhino_invest.runner import _watchdog_heartbeat


class BoundedWorkerTests(unittest.TestCase):
    def test_watchdog_has_no_network_or_paid_model_dependency(self):
        with patch.dict('os.environ',{},clear=True),patch('rhino_invest.runner.socket.socket') as channel:
            self.assertFalse(_watchdog_heartbeat());channel.assert_not_called()
        with patch.dict('os.environ',{'NOTIFY_SOCKET':'@unit-test'},clear=True),patch('rhino_invest.runner.socket.socket') as channel:
            self.assertTrue(_watchdog_heartbeat())
            channel.return_value.__enter__.return_value.sendto.assert_called_once_with(b'WATCHDOG=1','\0unit-test')

    def test_filtered_pages_do_not_decode_large_irrelevant_history(self):
        with Store(':memory:') as store:
            for i in range(80):
                store.put('recommendation',f'old-{i:03}',{'account':'real','status':'blocked','data':'x'*10000})
            for i in range(5):
                store.put('recommendation',f'paper-{i}',{'account':'paper','status':'actionable'})
            original=json.loads
            with patch('rhino_invest.store.json.loads',wraps=original) as decode:
                found=list(store.iter_items('recommendation',filters={'account':'paper','status':'actionable'},page_size=2))
            self.assertEqual(len(found),5);self.assertEqual(decode.call_count,5)

    def test_identity_fence_query_keeps_pending_and_unknown_operations(self):
        with Store(':memory:') as store:
            for key,state in [('a','delivered'),('b','pending'),('c','unknown')]:
                store.put('feishu_outbox',key,{'identity':'record:one','status':state})
            store.put('feishu_outbox','d',{'identity':'record:other','status':'unknown'})
            self.assertEqual([key for key,_ in store.iter_items('feishu_outbox',
                filters={'identity':'record:one','status':['pending','unknown']})],['b','c'])

    def test_page_iterator_does_not_hold_shared_lock_while_consumer_runs(self):
        with Store(':memory:') as store:
            store.put('sample','a',{'status':'ready'});store.put('sample','b',{'status':'ready'})
            iterator=store.iter_items('sample',page_size=1);next(iterator)
            done=threading.Event()
            thread=threading.Thread(target=lambda:(store.put('sample','z',{'status':'new'}),done.set()))
            thread.start();self.assertTrue(done.wait(2));thread.join()
            self.assertEqual([key for key,_ in iterator],['b'])

    def test_new_video_defers_paid_plan_until_independent_fundamentals_arrive(self):
        f=fixtures.ResearchTests();f.setUp();self.addCleanup(f.doCleanups)
        calls=[];original=fixtures.FixtureModel()
        class Model:
            def run(self,purpose,payload,**kwargs):
                calls.append(purpose);return original.run(purpose,payload,**kwargs)
        first=ingest_archives(f.store,f.db,model=Model(),fundamentals={},now=fixtures.NOW,
                             defer_missing_fundamentals=True)
        self.assertEqual(first['errors'],[])
        self.assertEqual(first['deferred_missing_fundamentals'],['NVDA'])
        self.assertNotIn('compile_plan',calls)
        before=len(calls)
        next_result=ingest_archives(f.store,f.db,model=Model(),fundamentals={'NVDA':{
            'financial_report':{'period':'2025','currency':'USD'}}},now=fixtures.NOW,
            defer_missing_fundamentals=True)
        self.assertEqual(next_result['errors'],[])
        self.assertNotIn('extract_levels',calls[before:]);self.assertIn('compile_plan',calls[before:])

    def test_new_video_focus_layout_is_frozen_before_provider_calls(self):
        f=fixtures.ResearchTests();f.setUp();self.addCleanup(f.doCleanups)
        model=fixtures.FixtureModel()
        ingest_archives(f.store,f.db,model=model,now=fixtures.NOW)
        layouts=f.store.items('research_extraction_layout')
        self.assertEqual(len(layouts),1)
        self.assertEqual(layouts[0][1],{'policy':'accepted_claim_frames','frame_seconds':[100]})
        ingest_archives(f.store,f.db,model=model,now=fixtures.NOW)
        self.assertEqual(f.store.items('research_extraction_layout'),layouts)


if __name__=='__main__':unittest.main()
