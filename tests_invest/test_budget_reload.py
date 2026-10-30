import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from rhino_invest.budget import ModelBudgetConfigError, budget_status, write_limits
from rhino_invest.config import Config
from rhino_invest.model import ModelGateway, ModelBudgetExceeded, UnknownModelOutcome
from rhino_invest.store import Store
from rhino_invest.__main__ import main


class BudgetReloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'budget.json'
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.config = {'enabled': True, 'model': 'fixture', 'base_url': 'https://model.invalid',
            'budget_file': str(self.path), 'max_calls_per_day': 99,
            'max_cost_usd_per_day': '99', 'max_cost_usd_per_call': '0.10'}
        self.sent = []
        self.now = '2026-10-24T04:00:00+00:00'
        self.gateway = ModelGateway(self.store, self.config, self.transport, clock=lambda: self.now)
        self.set_limits(1, '0.10')

    def transport(self, body):
        self.sent.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}

    def set_limits(self, calls, cost):
        return write_limits(self.path, {'max_calls_per_day': calls, 'max_cost_usd_per_day': cost})

    def test_same_gateway_raises_then_lowers_without_reset_or_cache_change(self):
        identity = self.gateway.cache_identity()
        self.gateway.run('extract', {'id': 1})
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.run('extract', {'id': 2})
        self.set_limits(3, '0.30')
        self.gateway.run('extract', {'id': 2})
        self.assertEqual(self.gateway.cache_identity(), identity)
        self.assertTrue(self.gateway.run('extract', {'id': 1})['ok'])
        self.set_limits(1, '0.10')
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.run('extract', {'id': 3})
        status = budget_status(self.store, self.config, now=self.now)
        self.assertEqual((status['calls_used'], status['calls_remaining']), (2, 0))
        self.assertEqual(status['reserved_cost_usd'], '0.20')
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.store.items('model_receipt')), 2)

    def test_cost_limit_is_independent_of_call_limit(self):
        self.set_limits(9, '0.15')
        self.gateway.run('extract', {'id': 1})
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.run('extract', {'id': 2})
        self.set_limits(9, '0.20')
        self.gateway.run('extract', {'id': 2})
        self.assertEqual(len(self.sent), 2)

    def test_inflight_keeps_reservation_next_call_observes_new_limit(self):
        def change_during_request(body):
            self.set_limits(1, '0.01')
            return self.transport(body)
        self.gateway.transport = change_during_request
        self.gateway.run('extract', {'id': 1})
        receipt = self.store.items('model_receipt')[0][1]
        self.assertEqual(receipt['reserved_cost_usd'], '0.10')
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.run('extract', {'id': 2})
        self.assertEqual(len(self.sent), 1)

    def test_invalid_file_pauses_paid_calls_but_allows_cached_work_and_recovery(self):
        self.gateway.run('extract', {'id': 1})
        for content in ('{', '[]', '{"max_calls_per_day":true,"max_cost_usd_per_day":"6"}',
                        '{"max_calls_per_day":60,"max_cost_usd_per_day":"NaN"}',
                        '{"max_calls_per_day":60,"max_cost_usd_per_day":"6","api_key":"private"}',
                        '{"max_calls_per_day":1,"max_calls_per_day":60,"max_cost_usd_per_day":"6"}',
                        'x' * 4097):
            with self.subTest(content=content[:50]):
                self.path.write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(ModelBudgetConfigError, 'paused'):
                    self.gateway.run('extract', {'id': 2})
                self.assertTrue(self.gateway.run('extract', {'id': 1})['ok'])
                self.assertEqual(budget_status(self.store, self.config, now=self.now)['status'], 'invalid')
        self.path.unlink()
        with self.assertRaises(ModelBudgetConfigError):
            self.gateway.run('extract', {'id': 2})
        self.assertEqual(len(self.sent), 1)
        self.set_limits(2, '0.20')
        self.gateway.run('extract', {'id': 2})
        self.assertEqual(len(self.sent), 2)

    def test_budget_change_does_not_bypass_unknown_outcome(self):
        def timeout(_):
            raise TimeoutError()
        self.gateway.transport = timeout
        with self.assertRaises(UnknownModelOutcome):
            self.gateway.run('extract', {'id': 1}, call_scope='same-source')
        self.set_limits(90, '9')
        self.gateway.transport = self.transport
        with self.assertRaises(UnknownModelOutcome):
            self.gateway.run('extract', {'id': 1}, call_scope='same-source')
        self.assertEqual(self.sent, [])
        self.assertEqual(budget_status(self.store, self.config, now=self.now)['calls_used'], 1)

    def test_budget_day_uses_utc_and_resets_naturally(self):
        self.now = '2026-10-24T23:59:00+00:00'
        self.gateway.run('extract', {'id': 1})
        self.now = '2026-10-25T07:59:30+08:00'
        with self.assertRaises(ModelBudgetExceeded):
            self.gateway.run('extract', {'id': 2})
        self.now = '2026-10-25T08:00:00+08:00'
        self.gateway.run('extract', {'id': 2})
        self.assertEqual(budget_status(self.store, self.config, now=self.now)['calls_used'], 1)
        self.assertEqual(len(self.store.items('model_call')), 2)

    def test_cli_preview_then_atomic_update_without_paid_request(self):
        env = {'INVEST_DATABASE': str(Path(self.folder.name) / 'cli.sqlite'),
            'INVEST_MODEL_BUDGET_FILE': str(self.path), 'INVEST_MODEL_MAX_COST_USD_PER_CALL': '0.10'}
        with Store(env['INVEST_DATABASE']):
            pass
        self.assertEqual(Config.load(environ=env).model['budget_file'], str(self.path))
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['budget', '--calls', '60', '--cost-usd', '6']), 0)
            self.assertFalse(json.loads(output.getvalue())['applied'])
            self.assertEqual(json.loads(self.path.read_text())['max_calls_per_day'], 1)
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['budget', '--calls', '60', '--cost-usd', '6', '--execute']), 0)
            value = json.loads(output.getvalue())
            self.assertTrue(value['applied'])
            self.assertFalse(value['usage_reset'])
            self.assertEqual(value['limits']['max_calls_per_day'], 60)
        self.assertEqual(self.sent, [])

    def test_cli_wrong_database_does_not_create_zero_usage_or_change_budget(self):
        database = Path(self.folder.name) / 'missing.sqlite'
        before = self.path.read_bytes()
        env = {'INVEST_DATABASE': str(database), 'INVEST_MODEL_BUDGET_FILE': str(self.path)}
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['budget', '--calls', '90', '--cost-usd', '9', '--execute']), 1)
        self.assertFalse(database.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_cannot_replace_ledger_even_if_budget_points_to_it(self):
        database = Path(self.folder.name) / 'ledger.json'
        with Store(database) as store:
            store.put('fixture', 'important', {'preserve': True})
        before = database.read_bytes()
        env = {'INVEST_DATABASE': str(database), 'INVEST_MODEL_BUDGET_FILE': str(database)}
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['budget', '--calls', '90', '--cost-usd', '9', '--execute']), 1)
        self.assertEqual(database.read_bytes(), before)
        with Store(database) as store:
            self.assertEqual(store.get('fixture', 'important'), {'preserve': True})

    def test_writer_refuses_other_files_and_preserves_them(self):
        for content in ('secret configuration', '{"model":{"api_key_env":"SECRET"}}', '{'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding='utf-8')
                with self.assertRaises(ModelBudgetConfigError):
                    self.set_limits(60, '6')
                self.assertEqual(self.path.read_text(encoding='utf-8'), content)
        self.path.unlink()
        self.set_limits(60, '6')
        before = self.path.read_bytes()
        with self.assertRaises(ModelBudgetConfigError):
            write_limits(self.path, {'max_calls_per_day': 90, 'max_cost_usd_per_day': '9'},
                         protected_paths=[self.path])
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_atomic_write_preserves_previous_file(self):
        before = self.path.read_bytes()
        with patch('rhino_invest.budget.os.replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.set_limits(90, '9')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.model-budget-*')), [])

    def test_concurrent_gateways_share_one_durable_daily_reservation(self):
        database = Path(self.folder.name) / 'shared.sqlite'
        first, second = Store(database), Store(database)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)
        def request(store, ident):
            gateway = ModelGateway(store, self.config, self.transport, clock=lambda: self.now)
            barrier.wait(timeout=5)
            try:
                gateway.run('extract', {'id': ident})
                return 'accepted'
            except ModelBudgetExceeded:
                return 'limited'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(request, first, 1), pool.submit(request, second, 2)
            self.assertCountEqual([a.result(timeout=10), b.result(timeout=10)], ['accepted', 'limited'])
        self.assertEqual(len(first.items('model_call')), 1)
        self.assertEqual(len(self.sent), 1)


if __name__ == '__main__':
    unittest.main()
