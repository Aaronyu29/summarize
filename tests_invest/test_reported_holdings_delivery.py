import threading
import unittest

from rhino_invest.config import Config
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio
from rhino_invest.reporting import (record_reported_holding, reported_holdings,
                                    with_reported_holdings, portfolio_rows, make_report)
from rhino_invest.runner import Runner, status
from rhino_invest.store import Store
from tests_invest.test_runner_delivery import Calendar, Feishu, Market


class ReportedHoldingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.at = '2026-10-23T12:00:00+00:00'
        self.now = '2026-10-23T20:10:00+00:00'

    def tearDown(self):
        self.store.close()

    def report(self, symbol='GOOGL', amount='750', at=None):
        return record_reported_holding(self.store, symbol, amount, reported_at=at or self.at)

    def test_statements_never_become_quantity_cost_cash_or_verified_inventory(self):
        first = self.report()
        self.assertEqual(self.report(), first)  # idempotent exact statement
        self.assertEqual(first['source'], 'user_statement')
        self.assertIsNone(first['quantity'])
        self.assertEqual(self.store.events('ledger'), [])
        self.assertEqual(self.store.items('ledger_staged'), [])
        raw = portfolio(self.store, now=self.now)
        snapshot = with_reported_holdings(self.store, raw)
        for key in ('cash', 'market_value', 'total_equity', 'holdings_complete', 'positions'):
            self.assertEqual(snapshot[key], raw[key])
        self.assertFalse(snapshot['holdings_complete'])
        self.assertIsNone(snapshot['total_equity'])
        row = portfolio_rows(snapshot)[0]
        self.assertEqual(row['系统标识'], 'real:GOOGL')
        self.assertIn('用户报告 750 USD', row['市值'])
        for field in ('数量', '成本', '市价', '未实现盈亏', '持有天数', '占总资产比例'):
            self.assertEqual(row[field], '未知／未配置')
        self.assertEqual(row['更新时间'], self.at)
        self.assertIn('未计入账本及资产合计', row['数据状态'])

    def test_new_statements_keep_history_and_respect_asof(self):
        old = self.report()
        new = self.report(amount='760', at='2026-10-24T12:00:00+00:00')
        self.assertNotEqual(old['id'], new['id'])
        self.assertEqual(len(self.store.items('reported_holding')), 2)
        self.assertEqual(reported_holdings(self.store, now=self.now), [old])
        self.assertEqual(reported_holdings(self.store, now='2026-10-25T00:00:00Z'), [new])
        with self.assertRaises(ValueError):
            self.report(amount='999')  # cannot overwrite an immutable statement
        for amount in ('NaN', '-1', True):
            with self.assertRaises(ValueError):
                self.report(amount=amount)

    def test_confirmed_ledger_replaces_same_row_without_erasing_statement_history(self):
        self.report()
        before = portfolio_rows(with_reported_holdings(self.store, portfolio(self.store, now=self.now)))[0]
        stage_entry(self.store, {'id': 'opening-google', 'account': 'real', 'type': 'opening_position',
            'symbol': 'GOOGL', 'quantity': '3', 'price': '200', 'fee': '0', 'currency': 'USD', 'timestamp': self.at})
        confirm_entry(self.store, 'opening-google')
        rows = portfolio_rows(with_reported_holdings(self.store, portfolio(self.store, now=self.now)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['系统标识'], before['系统标识'])
        self.assertEqual(rows[0]['数量'], '3')
        self.assertNotIn('用户报告', rows[0]['市值'])
        self.assertEqual(len(self.store.items('reported_holding')), 1)
        stage_entry(self.store, {'id': 'sell-google', 'account': 'real', 'type': 'sell',
            'symbol': 'GOOGL', 'quantity': '3', 'price': '250', 'fee': '0', 'currency': 'USD',
            'timestamp': '2026-10-23T13:00:00Z'})
        confirm_entry(self.store, 'sell-google')
        after = portfolio_rows(with_reported_holdings(self.store, portfolio(self.store, now=self.now)))
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['数量'], '0')  # old statement cannot resurrect a sold position

    def test_runner_tracks_reported_symbols_and_exports_timed_unknowns_separately(self):
        self.report()
        self.report('CRCL', '100')
        feishu, market = Feishu(), Market()
        runner = Runner(self.store, Config(symbols=()), market=market, feishu=feishu,
                        calendar=Calendar(), execute=True, clock=lambda: self.now)
        result = runner.run_once()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['quotes'], ['CRCL', 'GOOGL'])
        rows = [fields for table, fields in feishu.calls if table == '持仓总览']
        self.assertEqual({r['股票'] for r in rows}, {'CRCL', 'GOOGL'})
        self.assertTrue(all(r['市价'] == '未知／未配置' and r['更新时间'] == self.at for r in rows))
        self.assertEqual(result['portfolio']['positions'], [])
        self.assertFalse(result['portfolio']['holdings_complete'])
        self.assertIsNone(result['portfolio']['total_equity'])
        self.assertEqual(self.store.events('ledger'), [e for e in self.store.events('ledger') if e['account'] == 'paper'])
        self.assertEqual(len(status(self.store)['portfolio']['reported_holdings']), 2)
        report = make_report('每日复盘', 'fixture', result['portfolio'], now=self.now)
        self.assertIn(self.at, report['body'])
        self.assertIn('用户报告 750 USD', report['body'])
        self.assertIn('待核对', report['body'])
        paper_rows = portfolio_rows(with_reported_holdings(self.store, portfolio(self.store, 'paper', now=self.now)))
        self.assertEqual(paper_rows, [])


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.runner = Runner(self.store, Config(), market=Market(), feishu=Feishu(),
                             calendar=Calendar(), clock=lambda: '2026-10-23T20:10:00Z')
        self.stop = threading.Event()
        self.stop.set()

    def tearDown(self):
        self.store.close()

    def test_shutdown_waits_for_inflight_receipt_before_returning(self):
        owner = self
        class Inflight:
            alive = True
            def is_alive(self):
                return self.alive
            def join(self, timeout=None):
                owner.assertEqual(timeout, 660)
                owner.assertEqual(owner.store.get('worker_shutdown', 'latest')['status'], 'draining')
                owner.store.put('fixture_receipt', 'request', {'received': True})
                self.alive = False
        self.runner._research_thread = Inflight()
        self.runner.serve(stop=self.stop)
        self.assertTrue(self.store.get('fixture_receipt', 'request')['received'])
        self.assertEqual(self.store.get('worker_shutdown', 'latest')['status'], 'completed')

    def test_exceeding_grace_records_unknown_without_abandoning_request(self):
        owner = self
        class SlowInflight:
            alive = True
            calls = []
            def is_alive(self):
                return self.alive
            def join(self, timeout=None):
                self.calls.append(timeout)
                if timeout is None:
                    state = owner.store.get('worker_shutdown', 'latest')
                    owner.assertEqual(state['status'], 'inflight_outcome_unknown')
                    owner.assertTrue(state['waiting_for_receipt'])
                    self.alive = False
        thread = SlowInflight()
        self.runner._research_thread = thread
        self.runner.serve(stop=self.stop)
        self.assertEqual(thread.calls, [660, None])
        self.assertEqual(self.store.get('worker_shutdown', 'latest')['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
