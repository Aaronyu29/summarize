import json
import pathlib
import tempfile
import unittest
from finance_alerts import enqueue, flush, reason


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = pathlib.Path(__file__).resolve().parent.parent / ".cache" / "finance-tests"
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def test_publication_reason_and_confirmed_delivery_are_durable_and_deduplicated(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as temp:
            source = {'video_id': '4_uytm7I_XQ', 'title': '测试视频', 'published_utc': '2026-10-22T18:10:36Z'}
            args = (temp, source, '自动分析', ValueError('Second-pass review rejected'), '已暂停，需核实')
            path = enqueue(*args)
            text = json.loads(path.read_text(encoding='utf-8'))['text']
            self.assertIn('2026-10-23 02:10:36（北京时间）', text)
            self.assertIn('二次审核未通过', text)
            class Session:
                calls = 0
                def post(self, *a, **kw):
                    self.calls += 1
                    self.payload = kw['json']
                    return type('Response', (), {'status_code': 200, 'json': lambda _: {'code': 0}})()
            session = Session()
            self.assertEqual(flush(temp, {'LARK_WEBHOOK_URL': 'https://example.test'}, session)['sent'], 1)
            enqueue(*args)
            flush(temp, {'LARK_WEBHOOK_URL': 'https://example.test'}, session)
            self.assertEqual(session.calls, 1)

    def test_notification_timeout_is_retained_for_retry_and_secrets_are_excluded(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as temp:
            path = enqueue(temp, {}, '上传飞书', RuntimeError('PRIVATE_SECRET https://secret.test/token'), '重试')
            class Session:
                def post(self, *a, **kw):
                    raise TimeoutError('PRIVATE_WEBHOOK')
            self.assertEqual(flush(temp, {'LARK_WEBHOOK_URL': 'https://example.test'}, Session())['pending'], 1)
            saved = path.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE', saved)
            self.assertNotIn('secret.test', saved)
            self.assertIn('未知', saved)
            self.assertEqual(json.loads(saved)['status'], 'retry')
            self.assertIn('99991672', reason(RuntimeError('API business code 99991672')))


if __name__ == '__main__':
    unittest.main()
