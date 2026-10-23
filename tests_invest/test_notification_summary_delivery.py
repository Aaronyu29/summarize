import unittest

from rhino_invest.config import Config
from rhino_invest.reporting import make_report, notification_text, plan_row, report_row
from rhino_invest.runner import Runner
from rhino_invest.store import Store
from tests_invest.test_runner_delivery import Calendar, Feishu, Market


NOW = '2026-10-23T15:00:00+00:00'


class NotificationSummaryTests(unittest.TestCase):
    def snapshot(self):
        return {'account': 'real', 'cash': '20000', 'positions': [], 'holdings_complete': False,
                'cost_complete': False, 'fees_complete': False, 'reported_holdings': [
                    {'symbol': 'GOOGL', 'reported_market_value': '750', 'reported_at': '2026-10-23T12:00:00Z'}]}

    def plan(self, status='needs_review'):
        return {'id': 'b' * 64, 'symbol': 'TSM', 'revision': 1, 'status': status, 'horizon_months': 24,
                'next_review_at': '2027-01-01T00:00:00Z', 'entry': {'low': '360.123456', 'high': '406.987654'},
                'targets': [{'price': '540.123456', 'horizon_months': 24}],
                'stop': {'price': '306.123456', 'confirmation': 'daily_close'},
                'take_profit': [{'price': '500.12345', 'fraction': '0.5'}, {'price': '540.123456', 'fraction': '0.5'}],
                'thesis': 'raw unapproved internal thesis', 'evidence_refs': ['c' * 64]}

    def advice(self):
        return {'id': 'd' * 64, 'plan_id': 'b' * 64, 'revision': 1, 'symbol': 'TSM', 'account': 'real',
                'status': 'needs_review', 'action': 'watch', 'quantity': None,
                'expires_at': '2026-10-23T20:00:00Z',
                'reasons': ['schema.missing.some_internal_field', {'raw': False}],
                'scenario_returns': [{'annualized_return_percent': '987.654321'}]}

    def test_pending_plan_never_leaks_draft_prices_schema_hash_period_or_raw_flags(self):
        report = make_report('重大变化', 'a' * 64, self.snapshot(), [self.plan()], [self.advice()], now=NOW)
        text = report['notification_body']
        self.assertIn('23:00（北京时间）', text)
        self.assertIn('$20,000.00', text)
        self.assertIn('GOOGL 用户报市值$750.00，待核对', text)
        self.assertIn('1项计划待审核', text)
        self.assertIn('暂无新增操作建议', text)
        for forbidden in ('360', '406', '540', '306', '987', 'schema', 'None', 'False', 'needs_review',
                          'raw unapproved', 'a' * 64, 'b' * 64, 'c' * 64, 'd' * 64, '{', '['):
            self.assertNotIn(forbidden, text)
        self.assertIn('schema.missing', report['body'])  # audit retained internally
        self.assertEqual(report_row(report)['正文'], text)
        self.assertLessEqual(len(text.splitlines()), 8)
        row = plan_row(self.plan(), self.advice())
        for field in ('买入区间', '目标情景', '止损价', '情景年化收益估算'):
            self.assertEqual(row[field], '待审核，暂不展示')

    def test_current_action_has_reason_bounded_prices_and_horizon_without_internal_details(self):
        advice = dict(self.advice(), status='actionable', action='buy', quantity='2', amount_usd='1000',
                      reasons=['verified_entry_zone_and_reserved_cash'])
        report = make_report('重大变化', 'hash-period', self.snapshot(), [self.plan('active')], [advice], now=NOW)
        text = report['notification_body']
        for expected in ('TSM 首次买入', '2.00股', '$1,000.00', '$360.12', '$406.99', '$306.12', '$540.12', '24个月',
                         '原因：', '$500.12卖出50%', '常规交易日收盘确认', '现持天数待补', '建议有效至'):
            self.assertIn(expected, text)
        self.assertLessEqual(len(text.splitlines()), 12)
        self.assertNotIn('987.654321', text)
        for plans in ([self.plan()], [dict(self.plan('active'), revision=2)]):
            rendered = notification_text(report, current_plans=plans, recommendations=[advice])
            self.assertNotIn('$540', rendered)
            self.assertIn('暂无新增操作建议', rendered)
        expired = notification_text(report, current_plans=[self.plan('active')], recommendations=[advice], now='2026-10-24T00:00:00Z')
        self.assertNotIn('$540', expired)

    def test_watch_uses_structured_levels_and_chinese_times_never_raw_body(self):
        report = {'kind': '价格观察', 'timestamp': NOW, 'body': 'RAW_HASH_SCHEMA_PRIVATE_' + 'f' * 64}
        event = {'symbol': 'SOXX', 'touch_observed': True, 'price': '202.456789', 'quote_time': NOW,
                 'zone': {'low': '200.12345', 'high': '205.34567'}}
        text = notification_text(report, watch_event=event)
        self.assertIn('$202.46', text)
        self.assertIn('$200.12–$205.35', text)
        self.assertIn('不构成买卖建议', text)
        self.assertIn('23:00（北京时间）', text)
        self.assertNotIn('RAW', text)
        self.assertNotIn('RAW', notification_text(report))

    def test_runner_sanitizes_legacy_backlog_and_suppresses_per_stock_pending_alerts(self):
        with Store(':memory:') as store:
            feishu = Feishu()
            delivered = []
            feishu.notify = lambda key, text: delivered.append((key, text)) or {'status': 'provider_ack'}
            for index in range(4):
                advice = dict(self.advice(), id=f'old-advice-{index}')
                store.put('recommendation', advice['id'], advice)
                store.put('report', f'old-report-{index}', {'id': f'old-report-{index}', 'kind': '重大变化',
                    'timestamp': NOW, 'period': 'private-hash-period', 'account': 'real', 'portfolio': self.snapshot(),
                    'body': 'RAW_LEAK_schema_360_540_None_False', 'recommendation_ids': [advice['id']]})
            store.put('report', 'old-daily', {'id': 'old-daily', 'kind': '每日复盘', 'timestamp': NOW, 'period': 'private-hash-period',
                'account': 'real', 'portfolio': self.snapshot(), 'body': 'RAW_LEAK_schema_360_540_None_False'})
            runner = Runner(store, Config(symbols=()), market=Market(), feishu=feishu,
                            calendar=Calendar(), execute=True, clock=lambda: NOW)
            result = runner.run_once()
            self.assertEqual(result['errors'], [])
            self.assertFalse(any(key.startswith('old-report') for key, text in delivered))
            self.assertTrue(any('每日复盘' in text for key, text in delivered))
            for key, text in delivered:
                for forbidden in ('RAW_LEAK', 'schema', 'None', 'False', '360', '540'):
                    self.assertNotIn(forbidden, text)
            before = len(delivered)
            runner.run_once()
            self.assertEqual(len(delivered), before)

    def test_scheduled_backlog_keeps_audit_and_only_delivers_latest_per_kind_account(self):
        with Store(':memory:') as store:
            feishu, delivered = Feishu(), []
            feishu.notify = lambda key, text: delivered.append(key) or {'status': 'provider_ack'}
            for day in range(20, 23):
                for account in ('real', 'paper'):
                    key = f'old-{day}-{account}'
                    store.put('report', key, {'id': key, 'kind': '每日复盘', 'period': f'2026-10-{day}',
                        'timestamp': f'2026-10-{day}T20:00:00Z', 'account': account,
                        'portfolio': dict(self.snapshot(), account=account), 'body': 'full-private-audit'})
            runner = Runner(store, Config(symbols=()), market=Market(), feishu=feishu,
                            calendar=Calendar(), execute=True, clock=lambda: NOW)
            runner.run_once()
            self.assertFalse(any(key.startswith('old-') for key in delivered))
            for day in range(20, 23):
                for account in ('real', 'paper'):
                    key = f'old-{day}-{account}'
                    self.assertEqual(store.get('notification', key)['status'], 'suppressed')
                    self.assertEqual(store.get('report', key)['body'], 'full-private-audit')
            runner.run_once()
            self.assertEqual(len(delivered), 4)  # newest daily and weekly for each separate account

    def test_transient_operations_errors_do_not_accumulate_across_healthy_ticks(self):
        with Store(':memory:') as store:
            feishu, delivered = Feishu(), []
            feishu.notify = lambda key, text: delivered.append(text) or {'status': 'provider_ack'}
            runner = Runner(store, Config(), feishu=feishu, market=Market(), execute=True)
            bad = {'errors': [{'stage': 'official_quotes', 'error': 'ConnectionError'}]}
            good = {'errors': []}
            runner._observe_health(good, NOW)
            runner._observe_health(bad, NOW)
            runner._observe_health(good, NOW)
            runner._observe_health(bad, NOW)
            self.assertEqual(delivered, [])
            runner._observe_health(bad, NOW)
            self.assertEqual(len(delivered), 1)
            self.assertIn('行情暂不可用', delivered[0])
            self.assertNotIn('ConnectionError', delivered[0])
            self.assertEqual(len(delivered[0].splitlines()), 3)
            runner._observe_health(good, NOW)
            runner._observe_health(good, NOW)
            self.assertEqual(len(delivered), 2)


if __name__ == '__main__':
    unittest.main()
