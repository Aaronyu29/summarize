"""Offline checks for human handoff, delivery fences and evidence-bound corrections."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from finance_jobs import Jobs
from finance_manual_review import handoff, load_correction, repair
from finance_alerts import scan_missing_reports


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vid = 'abcdefghijk'
        self.capture = self.root/'videos'/self.vid
        self.directory = self.capture/self.vid
        self.directory.mkdir(parents=True)
        self.jobs = Jobs(self.root/'jobs.sqlite')
        self.addCleanup(self.jobs.db.close)
        self.jobs.add({'video_id':self.vid,'title':'原始视频','published_utc':'2026-10-28T01:59:05Z'},
                      status='needs_review',checkpoint={'capture':str(self.capture),'review_issues':['夜盘标注冲突']})
        image = self.directory/'frame.jpg';image.write_bytes(b'original screenshot')
        self.frame_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        self.write(self.directory/'frames.json',[{'timestamp_seconds':325,'path':'frame.jpg','sha256':self.frame_hash}])
        self.write(self.directory/'transcript.asr.json',{'segments':[{'id':1,'text':'盘后跌七个点'}]})
        self.candidate = {'overview':'**待核对**','claims':[{'headline':'盘别未标','text':'原始观点',
            'uncertainty':'待确认','frame_seconds':325,'evidence_segment_ids':['S000001']}],'review_questions':'支撑是否失守？'}
        self.receipt = self.directory/'candidate.response.json'
        self.write(self.receipt,{'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.candidate)}}]})
        self.write(self.capture/'analysis-review-state.json',{'candidate_file':self.receipt.name,'issues':['夜盘标注冲突']})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

    def correction(self):
        value={'source_candidate_file':self.receipt.name,
            'source_candidate_sha256':hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            'frame_seconds':325,'frame_sha256':self.frame_hash,'requires_independent_review':True,
            'changes':[{'claim_index':0,'field':'headline','before':'盘别未标','after':'夜盘跌7.37%'}]}
        self.write(self.capture/'user-correction.json',value)
        return value

    def test_draft_delivers_once_without_approving_or_modifying_original(self):
        original=self.receipt.read_bytes()
        publisher=Mock();publisher.return_value.run.return_value={'status':'delivered','url':'https://example.test/draft'}
        handoff(self.jobs,self.vid,self.root,{},publisher)
        handoff(self.jobs,self.vid,self.root,{},publisher)
        publisher.assert_called_once()
        job=self.jobs.get(self.vid)
        report=Path(job['checkpoint']['human_review_report']).read_text()
        self.assertIn('待人工审核',report);self.assertIn('夜盘标注冲突',report)
        self.assertIn('盘后跌七个点',report);self.assertIn('frame.jpg',report)
        self.assertEqual(job['status'],'needs_review')
        self.assertFalse((self.capture/'analysis.json').exists())
        self.assertFalse((self.root/'library.sqlite').exists())
        self.assertEqual(original,self.receipt.read_bytes())
        self.assertEqual(scan_missing_reports(self.root),0)

    def test_failed_publication_alert_is_merged_and_keeps_review_state(self):
        publisher=Mock(side_effect=TimeoutError('network'))
        for _ in range(2):
            self.assertEqual(handoff(self.jobs,self.vid,self.root,{},publisher)['status'],'review_document_failed')
        scan_missing_reports(self.root)
        self.assertEqual(len(list((self.root/'alerts').glob('*.json'))),1)
        self.assertEqual(self.jobs.get(self.vid)['status'],'needs_review')
        self.assertTrue(self.receipt.exists())

    def test_escaping_capture_path_is_held_and_alerted(self):
        self.jobs.save(self.vid,capture=str(self.root.parent))
        publisher=Mock()
        result=handoff(self.jobs,self.vid,self.root,{},publisher)
        self.assertEqual(result['status'],'review_document_failed');publisher.assert_not_called()
        self.assertEqual(len(list((self.root/'alerts').glob('*.json'))),1)

    def test_stale_processing_alert_does_not_change_job_or_alert_fresh_work(self):
        self.jobs.save(self.vid,status='processing')
        job=self.jobs.get(self.vid)
        self.assertEqual(scan_missing_reports(self.root,now=job['updated']+5399),0)
        self.assertEqual(scan_missing_reports(self.root,now=job['updated']+5401),1)
        self.assertEqual(self.jobs.get(self.vid),job)

    def test_correction_changes_only_explicit_field_and_preserves_receipt(self):
        original=self.receipt.read_bytes();self.correction()
        candidate,_=load_correction(self.capture,self.vid)
        self.assertEqual(candidate['claims'][0]['headline'],'夜盘跌7.37%')
        self.assertEqual(candidate['claims'][0]['text'],'原始观点')
        self.assertEqual(self.receipt.read_bytes(),original)
        for field,value in [('frame_sha256','wrong'),('requires_independent_review',False)]:
            correction=self.correction();correction[field]=value
            self.write(self.capture/'user-correction.json',correction)
            with self.assertRaises(ValueError):load_correction(self.capture,self.vid)
        correction=self.correction();correction['changes'][0]['before']='wrong'
        self.write(self.capture/'user-correction.json',correction)
        with self.assertRaises(ValueError):load_correction(self.capture,self.vid)

    def test_repeated_correction_attempt_cannot_generate_or_publish(self):
        self.correction();_,identity=load_correction(self.capture,self.vid)
        self.jobs.save(self.vid,user_correction_attempt_sha256=identity)
        with patch('finance_cloud.review_candidate') as review,patch('finance_cloud.finalize_analysis') as finalize:
            with self.assertRaisesRegex(ValueError,'already attempted'):
                repair(self.jobs,self.vid,self.root,{})
        review.assert_not_called();finalize.assert_not_called()


if __name__=='__main__':unittest.main()
