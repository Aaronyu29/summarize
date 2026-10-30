import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from finance_cloud import review_evidence, review_candidate, repair_frames
from finance_jobs import Jobs
from finance_alerts import scan_missing_reports
from finance_manual_review import load_correction, repair
import test_finance_manual_review as manual_tests


class ScopedReviewTests(unittest.TestCase):
    def test_completed_correction_returns_delivery_without_paid_replay_or_status_reset(self):
        fixture=manual_tests.ManualReviewTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.jobs.save(fixture.vid,status='complete',human_correction_verified=True,
                          delivery={'status':'delivered','url':'https://example.test/done'})
        before=fixture.jobs.get(fixture.vid)
        with patch('finance_cloud.review_candidate') as review,patch('finance_manual_review.Publisher') as publisher:
            self.assertEqual(repair(fixture.jobs,fixture.vid,fixture.root,{})['status'],'already_delivered')
        review.assert_not_called();publisher.assert_not_called()
        self.assertEqual(fixture.jobs.get(fixture.vid),before)
        fixture.jobs.save(fixture.vid,human_correction_verified=False)
        with self.assertRaisesRegex(ValueError,'cannot be reset'):
            repair(fixture.jobs,fixture.vid,fixture.root,{})
        self.assertEqual(fixture.jobs.get(fixture.vid)['status'],'complete')

    def test_citations_and_context_retained_without_unrelated_long_transcript(self):
        payload={'segments':[{'segment_id':f'S{i:06d}','start_seconds':i*10,
            'end_seconds':i*10+9,'text':'original'} for i in range(1000)],
            'available_frames':[10,300,700], 'visual_observations':[
                {'timestamp_seconds':i} for i in (10,300,700)]}
        claims=[{'evidence_segment_ids':['S000500'],'timestamp_seconds':300,'frame_seconds':700}]
        result=review_evidence(payload,claims)
        ids={s['segment_id'] for s in result['segments']}
        self.assertTrue({f'S{i:06d}' for i in range(498,503)} <= ids)
        self.assertIn('S000030',ids);self.assertIn('S000070',ids)
        self.assertNotIn('S000900',ids);self.assertEqual(result['available_frames'],[700])
        self.assertEqual(len(payload['segments']),1000)
        self.assertLess(len(result['segments']),30)

    def test_repairs_attach_original_and_disputed_replacement_frame(self):
        frames=[(55,Path('a')),(865,Path('b')),(1000,Path('c')),(2000,Path('d'))]
        self.assertEqual(repair_frames(['数据在约1000秒画面'],{'claims':[{'frame_seconds':865}]},frames),frames[1:3])

    def test_unchanged_batch_identity_reuses_but_evidence_change_invalidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);image=root/'a.jpg';image.write_bytes(b'original')
            video={'claims':[{'evidence_segment_ids':[0],'frame_seconds':55,'text':'test'}]}
            payload={'video_id':'test','segments':[{'segment_id':'S000000','text':'original'}]}
            names=[]
            def review(d,name,instruction,request,images,env):
                names.append(name)
                return {'accepted':True,'issues':[],'checked_claim_indices':[0],
                        'overview_checked':True,'review_questions_checked':True}
            with patch('finance_cloud.model_json',side_effect=review),patch('finance_cloud.effective_response_file',side_effect=lambda d,n:n):
                for _ in range(2):review_candidate(root,video,payload,[(55,image)],{})
                payload['segments'][0]['text']='changed evidence'
                review_candidate(root,video,payload,[(55,image)],{})
            self.assertEqual(names[:2],names[2:4]);self.assertNotEqual(names[0],names[4])
            self.assertNotEqual(names[1],names[5])

    def test_missing_report_alert_allows_import_but_still_detects_stalled_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);jobs=Jobs(root/'jobs.sqlite')
            jobs.add({'video_id':'abcdefghijk'},status='needs_review')
            updated=jobs.get('abcdefghijk')['updated']
            self.assertEqual(scan_missing_reports(root,now=updated+599),0)
            self.assertEqual(scan_missing_reports(root,now=updated+601),1)
            jobs.db.close()

    def test_frame_correction_checks_both_original_and_replacement_images(self):
        fixture=manual_tests.ManualReviewTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        value=fixture.correction();replacement=fixture.directory/'replacement.jpg'
        replacement.write_bytes(b'replacement')
        import hashlib
        frame_hash=hashlib.sha256(replacement.read_bytes()).hexdigest()
        fixture.write(fixture.directory/'frames.json',[
            {'timestamp_seconds':325,'path':'frame.jpg','sha256':fixture.frame_hash},
            {'timestamp_seconds':1000,'path':'replacement.jpg','sha256':frame_hash}])
        value['changes']=[{'claim_index':0,'field':'frame_seconds','before':325,'after':1000,
                          'frame_seconds':1000,'frame_sha256':frame_hash}]
        fixture.write(fixture.capture/'user-correction.json',value)
        candidate,_=load_correction(fixture.capture,fixture.vid)
        self.assertEqual(candidate['claims'][0]['frame_seconds'],1000)
        replacement.write_bytes(b'corrupt')
        with self.assertRaises(ValueError):load_correction(fixture.capture,fixture.vid)


if __name__=='__main__':unittest.main()
