import json
import unittest
from rhino_invest.store import Store
from rhino_invest.model import ModelGateway, UnknownModelOutcome, ModelBudgetExceeded, ModelOutputInvalid

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
        with self.assertRaises(ModelOutputInvalid):
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

    def test_deepseek_controls_are_explicit_and_change_response_fingerprint(self):
        calls = []
        def transport(body):
            calls.append(body)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'n': len(calls)})}}]}
        config = dict(CONFIG, model='deepseek-flash', max_calls_per_day=4, max_cost_usd_per_day='2')
        first = ModelGateway(self.store, config, transport).run('extract', {'source': 1}, call_scope='source-1')
        second = ModelGateway(self.store, dict(config, max_tokens=16000), transport).run('extract', {'source': 1}, call_scope='source-1')
        third = ModelGateway(self.store, dict(config, max_tokens=16000, thinking='enabled', reasoning_effort='low'), transport).run('extract', {'source': 1}, call_scope='source-1')
        self.assertEqual([first['n'], second['n'], third['n']], [1, 2, 3])
        self.assertEqual(calls[0]['thinking'], {'type': 'disabled'})
        self.assertEqual(calls[1]['max_tokens'], 16000)
        self.assertEqual(calls[2]['reasoning_effort'], 'low')
        with self.assertRaises(ValueError):
            ModelGateway(self.store, dict(config, thinking='disabled', reasoning_effort='low'), transport).run('bad', {})
        self.assertEqual(len(calls), 3)

    def test_known_truncated_receipt_can_retry_new_revision_without_deleting_history(self):
        calls = []
        def transport(body):
            calls.append(body)
            if len(calls) == 1:
                return {'choices': [{'finish_reason': 'length', 'message': {'content': '', 'reasoning_content': 'archived reasoning'}}],
                        'usage': {'completion_tokens': 12000, 'completion_tokens_details': {'reasoning_tokens': 12000}}}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
        with self.assertRaises(ModelOutputInvalid):
            ModelGateway(self.store, CONFIG, transport).run('extract', {'source': 1}, call_scope='same-source')
        previous = self.store.items('model_receipt')[0]
        recovered = ModelGateway(self.store, dict(CONFIG, configuration_revision=2, thinking='disabled'), transport).run(
            'extract', {'source': 1}, call_scope='same-source')
        self.assertTrue(recovered['ok'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.get('model_receipt', previous[0]), previous[1])
        self.assertEqual(len(self.store.items('model_receipt')), 2)

    def test_unknown_scope_survives_configuration_and_payload_migration(self):
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        with self.assertRaises(UnknownModelOutcome):
            ModelGateway(self.store, CONFIG, transport).run('extract', {'full': 'old source payload'}, call_scope='source-1')
        with self.assertRaises(UnknownModelOutcome):
            ModelGateway(self.store, dict(CONFIG, configuration_revision=2, thinking='disabled'), transport).run(
                'extract', {'compact': 'same source'}, call_scope='source-1')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.items('model_receipt'), [])

    def test_legacy_unknown_without_scope_is_not_bypassed_by_upgrade(self):
        self.store.put('model_call', 'old-request', {'id': 'old-request', 'timestamp': NOW,
            'purpose': 'extract', 'reserved_cost_usd': '0.5'}, immutable=True)
        calls = []
        with self.assertRaises(UnknownModelOutcome):
            ModelGateway(self.store, dict(CONFIG, configuration_revision=2), lambda body: calls.append(body)).run(
                'extract', {'compact': 'new layout'}, call_scope='source-1')
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
