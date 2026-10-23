import json
import unittest
from rhino_invest.store import Store
from rhino_invest.model import ModelGateway, UnknownModelOutcome, ModelBudgetExceeded

CONFIG = {'enabled': True, 'model': 'offline-fixture', 'base_url': 'https://model.invalid/v1',
    'max_cost_usd_per_call': '0.5', 'max_cost_usd_per_day': '1', 'max_calls_per_day': 2}
NOW = '2026-10-23T15:00:00+00:00'


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def test_response_cache_and_usage_preserve_no_extra_calls(self):
        calls = []
        def transport(body):
            calls.append(body)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"accepted":true}'}}],
                    'usage': {'prompt_tokens': 40, 'completion_tokens': 8}}
        model = ModelGateway(self.store, CONFIG, transport)
        self.assertTrue(model.run('review', {'id': 1}, now=NOW)['accepted'])
        model.run('review', {'id': 1}, now=NOW)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.items('model_receipt')[0][1]['usage']['prompt_tokens'], 40)
        model.run('review', {'id': 2}, now=NOW)
        with self.assertRaises(ModelBudgetExceeded):
            model.run('review', {'id': 3}, now=NOW)
        self.assertEqual(len(calls), 2)

    def test_timeout_fence_survives_gateway_restart_and_day(self):
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        with self.assertRaises(UnknownModelOutcome):
            ModelGateway(self.store, CONFIG, transport).run('extract', {'id': 1}, now=NOW)
        with self.assertRaises(UnknownModelOutcome):
            ModelGateway(self.store, CONFIG, transport).run('extract', {'id': 1}, now='2026-10-24T15:00:00+00:00')
        self.assertEqual(len(calls), 1)

    def test_invalid_paid_response_is_archived_and_fenced(self):
        def transport(body):
            return {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}], 'usage': None}
        model = ModelGateway(self.store, CONFIG, transport)
        with self.assertRaises(ValueError):
            model.run('extract', {}, now=NOW)
        self.assertEqual(len(self.store.items('model_receipt')), 1)
        with self.assertRaises(UnknownModelOutcome):
            model.run('extract', {}, now=NOW)

    def test_actual_clock_resets_budget_while_analysis_clock_stays_frozen(self):
        current = [NOW]
        calls = []
        def transport(body):
            calls.append(body)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}
        model = ModelGateway(self.store, dict(CONFIG, max_calls_per_day=1), transport, clock=lambda: current[0])
        model.run('compile', {'id': 1, 'as_of': NOW}, now=NOW)
        with self.assertRaises(ModelBudgetExceeded):
            model.run('compile', {'id': 2, 'as_of': NOW}, now=NOW)
        current[0] = '2026-10-24T15:00:00+00:00'
        model.run('compile', {'id': 2, 'as_of': NOW}, now=NOW)
        self.assertEqual(len(calls), 2)
        stamps = sorted(v['timestamp'] for _, v in self.store.items('model_call'))
        self.assertEqual(stamps, [NOW, current[0]])
        self.assertTrue(all(v['analysis_as_of'] == NOW for _, v in self.store.items('model_call')))


if __name__ == '__main__':
    unittest.main()
