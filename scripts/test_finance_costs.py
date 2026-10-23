import json
import pathlib
import tempfile
import unittest
from finance_costs import summarize


class CostsTests(unittest.TestCase):
    def setUp(self):
        root = pathlib.Path(__file__).resolve().parent.parent / '.cache' / 'finance-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.capture = pathlib.Path(self.temp.name) / 'videos' / '4_uytm7I_XQ'
        self.raw = self.capture / self.capture.name
        self.raw.mkdir(parents=True)

    def response(self, name, **changes):
        value = {'id': 'unique-call', 'model': 'deepseek-flash', 'created': 1792749600,
                 'usage': {'prompt_tokens': 1000, 'completion_tokens': 500,
                           'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 800,
                           'total_tokens': 1500, 'completion_tokens_details': {'reasoning_tokens': 400}}}
        value.update(changes)
        (self.raw/name).write_text(json.dumps(value), encoding='utf-8')

    def test_repair_calls_count_but_duplicate_response_and_reasoning_do_not_double_count(self):
        self.response('analysis.response.json')
        self.response('duplicate.response.json')
        self.response('analysis-repair.response.json', id='repair-call')
        result = summarize(self.capture)
        self.assertEqual(result['call_count'], 2)
        self.assertEqual(result['totals']['total_tokens'], 3000)
        self.assertEqual(result['totals']['completion_tokens'], 1000)
        self.assertEqual(result['calls'][0]['period'], 'offpeak')
        self.assertEqual(result['totals']['estimated_cost_cny'], '0.005608')

    def test_missing_usage_and_pending_requests_remain_unknown(self):
        self.response('unknown.response.json', usage={})
        (self.raw/'missing.pending.json').write_text('{}')
        result = summarize(self.capture)
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['unknown_usage_files'], ['unknown.response.json'])
        self.assertEqual(result['call_count'], 0)

    def test_peak_pricing_and_holiday_override(self):
        import datetime as dt
        moment = dt.datetime(2026,10,23,10,tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.response('analysis.response.json', created=int(moment.timestamp()))
        result = summarize(self.capture)
        self.assertEqual(result['calls'][0]['period'], 'peak')
        self.assertEqual(result['totals']['estimated_cost_cny'], '0.005608')


if __name__ == '__main__':
    unittest.main()
