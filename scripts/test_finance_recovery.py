"""Offline failure injection: bounded recovery must survive restarts without duplicate writes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests
from finance_recovery import (RecoveryDeferred, RecoveryExhausted, ReviewRejected,
    advisory_lock, atomic, pending_marker, policy, replacement)
from finance_cloud import analyze, handle_failure, main, model_json, pending_retry_at, review_candidate
from finance_jobs import Jobs
from finance_alerts import recover_interruption


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1]/'.cache/finance-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'FINANCE_AUTO_RECOVERY':'1', 'LLM_BASE_URL':'https://api.deepseek.com/v1',
            'LLM_MODEL':'deepseek-flash', 'LLM_API_KEY':'offline', 'FEISHU_APP_ID':'offline',
            'FEISHU_APP_SECRET':'offline', 'LARK_WEBHOOK_URL':'offline'}
        self.jobs = Jobs(self.root/'jobs.sqlite')
        self.addCleanup(self.jobs.db.close)
        self.vid = 'abcdefghijk'
        self.jobs.add({'video_id':self.vid,'title':'fixture','published_utc':'2026-10-27T00:00:00Z'})

    @staticmethod
    def response(content=None):
        return type('Response', (), {'status_code':200, 'json':lambda _: {
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content or {'accepted':True})}}]}})()

    def test_unknown_waits_then_one_replacement_is_cached_and_old_cost_stays_unknown(self):
        with patch('finance_cloud.update_costs'), patch('requests.post',side_effect=[requests.Timeout(),self.response()]) as post:
            with self.assertRaises(RecoveryDeferred):
                model_json(self.root,'analysis','instruction',{'a':1},[],self.env)
            parent=self.root/'analysis.pending.json'
            marker=json.loads(parent.read_text())
            with self.assertRaises(RecoveryDeferred):
                model_json(self.root,'analysis','instruction',{'a':1},[],self.env)
            self.assertEqual(post.call_count,1)
            marker['started_epoch']=time.time()-2000
            atomic(parent,marker)
            original=parent.read_bytes()
            for _ in range(3):
                self.assertEqual(model_json(self.root,'analysis','instruction',{'a':1},[],self.env),{'accepted':True})
            self.assertEqual(post.call_count,2)
            self.assertEqual(parent.read_bytes(),original)
            audit=json.loads(next(self.root.glob('*.transport-recovery.audit.json')).read_text())
            self.assertTrue(audit['parent_result_remains_unknown'])
            self.assertTrue(audit['possible_duplicate_billing'])
            self.assertIsNone(pending_retry_at(self.root,self.env))

    def test_replacement_failure_cannot_cascade_or_change_payload(self):
        with patch('finance_cloud.update_costs'),patch('requests.post',side_effect=requests.Timeout()) as post:
            with self.assertRaises(RecoveryDeferred):model_json(self.root,'analysis','i',{},[],self.env)
            parent=self.root/'analysis.pending.json'
            marker=json.loads(parent.read_text());marker['started_epoch']=time.time()-2000;atomic(parent,marker)
            with self.assertRaises(RecoveryDeferred):model_json(self.root,'analysis','i',{},[],self.env)
            child=self.root/'analysis-transport-recovery-001.pending.json'
            marker=json.loads(child.read_text());marker['started_epoch']=time.time()-2000;atomic(child,marker)
            for name in ('analysis','different-name'):
                with self.assertRaises(RecoveryExhausted):model_json(self.root,name,'i',{},[],self.env)
            self.assertEqual(post.call_count,2)

    def test_live_owner_never_gets_replacement_even_after_grace(self):
        marker=pending_marker('input','i',[]);marker['started_epoch']=time.time()-5000
        atomic(self.root/'analysis.pending.json',marker)
        with self.assertRaises(RecoveryDeferred):replacement(self.root,'analysis',marker,self.env,'input')

    def test_call_ceiling_counts_receipts_across_restarts(self):
        env=dict(self.env,FINANCE_MAX_CALLS_PER_VIDEO='8')
        for index in range(8):atomic(self.root/f'old-{index}.response.json',{})
        with patch('requests.post') as post, self.assertRaisesRegex(RecoveryExhausted,'ceiling'):
            model_json(self.root,'new','i',{},[],env)
        post.assert_not_called()

    def test_backoff_preserves_checkpoint_and_releases_other_videos(self):
        self.jobs.start(self.vid);self.jobs.save(self.vid,analysis_complete=True,report='saved')
        with patch('finance_cloud.failure_notice') as notice:
            self.assertFalse(handle_failure(self.jobs,self.vid,self.root,self.env,requests.Timeout()))
        notice.assert_not_called()
        job=self.jobs.get(self.vid)
        self.assertTrue(job['checkpoint']['analysis_complete'])
        self.assertEqual(self.jobs.pending(now=time.time()),[])
        self.assertEqual(self.jobs.pending(now=time.time()+301),[self.vid])
        self.jobs.add({'video_id':'newvideo123'})
        self.assertEqual(self.jobs.pending(now=time.time()),['newvideo123'])

    def test_review_and_attempt_exhaustion_are_terminal_with_reasons(self):
        self.jobs.start(self.vid)
        with patch('finance_cloud.failure_notice') as notice, patch('finance_manual_review.handoff',return_value=None):
            self.assertTrue(handle_failure(self.jobs,self.vid,self.root,self.env,ReviewRejected(['source conflict'])))
        notice.assert_called_once()
        self.assertEqual(self.jobs.get(self.vid)['checkpoint']['review_issues'],['source conflict'])
        self.assertEqual(self.jobs.pending(max_attempts=8),[])

    def test_untraceable_webhook_is_held_but_exact_image_target_can_reconcile(self):
        capture=self.root/'videos'/self.vid;capture.mkdir(parents=True)
        path=capture/'.feishu-state-fixture.json'
        for label,terminal in [('send_webhook',True),('create_image:marker',False),('create_import',True)]:
            atomic(path,{'pending_write':label})
            self.jobs.start(self.vid)
            with patch('finance_cloud.failure_notice'), patch('finance_manual_review.handoff',return_value=None):
                self.assertEqual(handle_failure(self.jobs,self.vid,self.root,self.env,RuntimeError('unknown')),terminal)

    def test_process_interruption_queues_without_notifying_or_losing_attempts(self):
        self.jobs.start(self.vid);self.jobs.save(self.vid,analysis_complete=True)
        with patch('finance_alerts.enqueue') as notice:
            recover_interruption(self.root,self.env,'timeout')
        notice.assert_not_called()
        job=self.jobs.get(self.vid)
        self.assertEqual(job['status'],'retry');self.assertEqual(job['attempts'],1)
        self.assertTrue(job['checkpoint']['analysis_complete'])

    def test_idle_recovery_does_not_scan_or_run_completed_video(self):
        self.jobs.save(self.vid,status='complete')
        with patch.dict(os.environ,self.env),patch('sys.argv',['worker','--data',str(self.root),'--retry-only']), \
             patch('finance_cloud.discover') as discover,patch('finance_cloud.process') as process, \
             patch('finance_cloud.flush_notices',return_value={}),patch('finance_cloud.Jobs',return_value=self.jobs):
            main()
        discover.assert_not_called();process.assert_not_called()

    def test_stale_publisher_lock_inode_can_be_reused_and_concurrency_is_blocked(self):
        path=self.root/'publisher.lock';path.write_text('crashed prior worker')
        with advisory_lock(path):
            with self.assertRaises(OSError):
                with advisory_lock(path):pass
        with advisory_lock(path):pass

    def test_claims_use_original_frames_and_global_review_is_separate(self):
        frames=[(i,self.root/f'{i}.jpg') for i in range(9)]
        for _, path in frames:path.write_bytes(b'original frame')
        video={'overview':'summary','review_questions':'follow-up','claims':[
            {'frame_seconds':i,'evidence_segment_ids':[i],'text':'condition'} for i in range(9)]}
        payload={'video_id':self.vid,'segments':[]}
        seen=[]
        def reviewer(directory,name,instruction,request,images,env):
            seen.append((name,request,images))
            if name.endswith('global'):
                return {'accepted':True,'issues':[],'overview_checked':True,'review_questions_checked':True,
                        'source_decisions_checked':True}
            return {'accepted':True,'issues':[],'checked_claim_indices':request['global_claim_indices']}
        with patch('finance_cloud.model_json',side_effect=reviewer),patch('finance_cloud.effective_response_file',side_effect=lambda d,n:n+'.response.json'):
            issues,receipts=review_candidate(self.root,video,payload,frames,self.env)
        self.assertEqual(issues,[]);self.assertEqual(len(receipts),3)
        self.assertEqual([len(entry[2]) for entry in seen],[8,1,8])
        self.assertNotIn('overview',seen[0][1]['candidate'])
        self.assertIn('overview',seen[-1][1]['candidate'])
        self.assertEqual(video['claims'][0]['evidence_segment_ids'],[0])
        self.assertEqual(seen[1][1]['candidate']['claims'][0]['claim_index'],8)
        self.assertEqual(seen[1][1]['global_claim_indices'],[8])

    def test_malformed_coverage_does_not_pass_or_crash_global_check(self):
        video={'claims':[{'evidence_segment_ids':[0],'frame_seconds':1}]}
        with patch('finance_cloud.model_json',return_value={'accepted':True,'issues':[],'checked_claim_indices':[[0]]}), \
             patch('finance_cloud.effective_response_file',return_value='receipt.json'):
            issues,_=review_candidate(self.root,video,{'video_id':self.vid},[],self.env)
        self.assertTrue(issues)

    def test_policy_cannot_enable_unbounded_paid_retries(self):
        for key,value in [('FINANCE_UNKNOWN_REPLACEMENTS','2'),('FINANCE_SEMANTIC_REPAIRS','99'),('FINANCE_MAX_CALLS_PER_VIDEO','1000')]:
            with self.assertRaises(ValueError):policy(dict(self.env,**{key:value}))

    def test_semantic_repair_reaudits_changed_candidate_and_stops_after_three(self):
        from test_finance_archive import EvidenceTests
        fixture=EvidenceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.capture;vid=fixture.vid
        atomic(root/'discovery.json',{'entries':[{'video_id':vid,'published_utc':'2026-10-27T00:00:00Z'}]})
        base=copy.deepcopy(fixture.analysis['videos'][0])
        base.update(overview='**保留条件判断**。\n逐条核对证据。',review_questions='本报告复盘补充：**若条件改变则下调判断**。')
        base['claims']=[dict(copy.deepcopy(base['claims'][0]),text='博主判断：**仅在条件成立时有效**。',evidence_segment_ids=['S000000']) for _ in range(8)]
        def model(directory,name,*args,**kwargs):
            if name.startswith('visual-'):return {'frames':[{'timestamp_seconds':15,'visible_facts':'fixture'}]}
            return dict(copy.deepcopy(base),overview='**保留条件判断**。\n修订轮次'+name)
        for failures in (2,4):
            with self.subTest(failures=failures),patch('finance_cloud.model_json',side_effect=model) as calls, \
                 patch('finance_cloud.effective_response_file',side_effect=lambda d,n:n+'.response.json'), \
                 patch('finance_cloud.review_candidate',side_effect=[(['source mismatch'],[]) for _ in range(failures)]+[([],['approved.json'])]), \
                 patch('finance_cloud.register_archive') as register,patch('finance_cloud.update_costs'):
                if failures==4:
                    with self.assertRaises(ReviewRejected):analyze(root,{'id':vid,'title':'fixture','duration':200},self.env)
                    self.assertEqual(sum('review-v2-repair' in c.args[1] for c in calls.call_args_list),3)
                else:
                    report=analyze(root,{'id':vid,'title':'fixture','duration':200},self.env)
                    self.assertTrue(report.is_file())
                    sealed=json.loads((root/'analysis-v1.json').read_text(encoding='utf-8'))
                    self.assertEqual(sealed['review_provenance']['semantic_repair_count'],2)
                    self.assertEqual(sealed['review_provenance']['verification_files'],['approved.json'])
                    manifest=json.loads((root/'manifest-v1.json').read_text(encoding='utf-8'))
                    expected=next(item['sha256'] for item in manifest['artifacts'] if item['path']==report.name)
                    self.assertEqual(expected,hashlib.sha256(report.read_bytes()).hexdigest())
                    register.assert_called_once()
                    self.assertEqual(sum('review-v2-repair' in c.args[1] for c in calls.call_args_list),2)


if __name__=='__main__':unittest.main()
