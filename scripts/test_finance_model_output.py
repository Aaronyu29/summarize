import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import requests
from finance_cloud import model_json
from finance_alerts import reason


class OutputRecoveryTests(unittest.TestCase):
    def setUp(self):
        root=pathlib.Path(__file__).resolve().parents[1]/'.cache/finance-tests'
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.directory=pathlib.Path(self.temp.name)
        self.env={'LLM_BASE_URL':'https://api.deepseek.com/v1','LLM_MODEL':'deepseek-flash','LLM_API_KEY':'offline-fixture'}
        self.payload={'video_id':'fixture','segments':[{'segment_id':'S000001','text':'source'}]}
        self.frame=self.directory/'frame.jpg';self.frame.write_bytes(b'offline-image-fixture')
        self.costs=patch('finance_cloud.update_costs');self.costs.start();self.addCleanup(self.costs.stop)

    def response(self,finish='stop',content='{"accepted":true}'):
        value={'id':'fixture-'+finish,'choices':[{'finish_reason':finish,'message':{'content':content}}],
               'usage':{'completion_tokens':24000 if finish=='length' else 10}}
        return type('Response',(),{'status_code':200,'json':lambda _:value})()

    def run_model(self,name='verification-reviewed-000'):
        return model_json(self.directory,name,'independent review',self.payload,[(10,self.frame)],self.env)

    def test_verification_keeps_bounded_reasoning_and_returns_decision(self):
        with patch('requests.post',return_value=self.response()) as post:
            self.assertEqual(self.run_model(),{'accepted':True})
        body=post.call_args.kwargs['json']
        self.assertEqual(body['thinking'],{'type':'enabled'})
        self.assertEqual(body['reasoning_effort'],'low')
        self.assertEqual(body['max_tokens'],24000)

    def test_completed_length_recovers_once_with_same_evidence_and_preserved_receipt(self):
        with patch('requests.post',side_effect=[self.response('length',''),self.response()]) as post:
            for _ in range(3):self.assertEqual(self.run_model(),{'accepted':True})
        self.assertEqual(post.call_count,2)
        first,second=[call.kwargs['json'] for call in post.call_args_list]
        self.assertEqual(first['messages'],second['messages'])
        self.assertEqual(second['thinking'],{'type':'disabled'})
        self.assertEqual(second['reasoning_effort'],'none')
        parent=self.directory/'verification-reviewed-000.response.json'
        self.assertEqual(json.loads(parent.read_text())['choices'][0]['finish_reason'],'length')
        audit=json.loads((self.directory/'verification-reviewed-000-output-recovery-001.audit.json').read_text())
        self.assertEqual(audit['parent_response_sha256'],hashlib.sha256(parent.read_bytes()).hexdigest())
        self.assertEqual(audit['frames'][0]['sha256'],hashlib.sha256(self.frame.read_bytes()).hexdigest())

    def test_second_length_is_terminal_across_replays(self):
        with patch('requests.post',side_effect=[self.response('length',''),self.response('length','')]) as post:
            for _ in range(3):
                with self.assertRaisesRegex(ValueError,'Incomplete model output'):self.run_model()
        self.assertEqual(post.call_count,2)

    def test_unknown_child_keeps_pending_fence(self):
        with patch('requests.post',side_effect=[self.response('length',''),requests.Timeout()]) as post:
            for _ in range(2):
                with self.assertRaises(RuntimeError):self.run_model()
        self.assertEqual(post.call_count,2)
        self.assertTrue((self.directory/'verification-reviewed-000-output-recovery-001.pending.json').exists())

    def test_parent_pending_and_unreadable_stop_are_not_recovery_authority(self):
        parent=self.directory/'verification-reviewed-000.response.json'
        parent.write_text(json.dumps(self.response('length','').json()))
        (self.directory/'verification-reviewed-000.pending.json').write_text('{}')
        with patch('requests.post') as post:
            with self.assertRaisesRegex(RuntimeError,'pending request'):self.run_model()
            post.assert_not_called()
        with patch('requests.post',return_value=self.response('stop','{bad json')) as post:
            with self.assertRaisesRegex(ValueError,'Incomplete model output'):self.run_model('analysis')
            self.assertEqual(post.call_count,2)

    def test_alert_names_output_limit(self):
        description=reason(ValueError('Incomplete model output; cached response requires review'))
        self.assertIn('输出上限',description)
        self.assertNotIn('ValueError',description)


if __name__=='__main__':unittest.main()
