import unittest
from unittest.mock import patch

from rhino_invest.config import Config
from rhino_invest.decisions import record_thesis_risk
from rhino_invest.ledger import stage_entry, confirm_entry
from tests_invest.plan_fixtures import seal_fixture_review as seal_plan_review
from rhino_invest.runner import Runner
from rhino_invest.store import Store
from tests_invest.test_acceptance_core import approved_plan, quote, FixtureCalendar
from tests_invest.test_runner_delivery import Calendar, Feishu, Market


NOW = '2026-10-23T15:00:00+00:00'


class NotificationMuteTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.config = Config(symbols=('SOXX',), notifications_enabled=False)
        self.feishu, self.market, self.sent = Feishu(), Market(), []
        self.feishu.notify = lambda key, text: self.sent.append((key, text)) or {'status': 'provider_ack'}
        self.runner = Runner(self.store, self.config, feishu=self.feishu, market=self.market,
                             calendar=Calendar(), execute=True, clock=lambda: NOW)

    def report(self, identity, kind):
        self.store.put('report', identity, {'id': identity, 'kind': kind, 'period': 'fixture',
            'timestamp': NOW, 'account': 'real', 'portfolio': {}, 'body': 'private audit',
            'risk_info': {'symbol': 'SOXX', 'reasons': ['confirmed_price_stop'], 'quantity': '2'},
            'recommendation_ids': []}, immutable=True)

    def test_muted_quotes_research_and_bitable_continue_all_report_routes_are_consumed(self):
        for kind in ('风险提示', '价格观察', '重大变化', '每日复盘', '每周复盘', '季度复核'):
            self.report('muted-' + kind, kind)
        self.config.archive_database = 'offline-archive'
        self.runner.model = object()
        self.store.put('research_worker', 'latest', {'status': 'blocked', 'errors': [{'error': 'ModelOutputInvalid'}]})
        with patch('rhino_invest.runner.ingest_archives', return_value={'ingested': 1, 'plans': [], 'errors': []}) as ingest:
            result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.market.quote_calls, 1)
        self.assertEqual(result['quotes'], ['SOXX'])
        ingest.assert_called_once()
        self.assertTrue(self.feishu.calls)  # Table projections still sync.
        self.assertEqual(self.sent, [])
        for identity, _ in self.store.items('report'):
            notice = self.store.get('notification', identity)
            self.assertEqual(notice['status'], 'suppressed')
            self.assertEqual(notice['reason'], 'notifications_disabled')
        self.assertTrue(all(v['status'] == 'suppressed' for _, v in self.store.items('ops_notice')))
        self.assertFalse(self.store.get('worker', 'latest')['notifications_enabled'])
        self.config.notifications_enabled = True
        with patch('rhino_invest.runner.ingest_archives', return_value={'plans': [], 'errors': []}):
            self.runner.run_once()
        self.assertEqual(self.sent, [])  # No report/ops backlog on enable.
        self.report('new-live-risk', '风险提示')
        with patch('rhino_invest.runner.ingest_archives', return_value={'plans': [], 'errors': []}):
            self.runner.run_once()
        self.assertEqual([key for key, _ in self.sent], ['new-live-risk'])

    def test_muted_operations_recovery_is_not_later_sent_as_old_acceptance_alarm(self):
        self.store.put('research_worker', 'latest', {'status': 'blocked', 'errors': [{'error': 'ValueError'}]})
        self.runner._observe_health({'errors': []}, NOW)
        self.config.notifications_enabled = True
        self.store.put('research_worker', 'latest', {'status': 'completed', 'errors': []})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.get('ops_health', 'latest')['status'], 'healthy')

    def held_risk(self):
        plan = approved_plan('SOXX')
        self.store.put('research_evidence', 'sealed:1', {'id': 'sealed:1'}, immutable=True)
        seal_plan_review(self.store, plan, {'accepted': True}, plan['evidence_refs'])
        self.store.put('plan', plan['id'], plan)
        self.store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
        for entry in ({'id': 'cash', 'type': 'opening_cash', 'amount': '20000'},
                      {'id': 'held', 'type': 'opening_position', 'symbol': 'SOXX', 'quantity': '2', 'price': '100', 'fee': '0'}):
            stage_entry(self.store, dict(entry, account='real', currency='USD', timestamp=NOW))
            confirm_entry(self.store, entry['id'])
        self.store.put('review_event_evidence', 'raw-fact', {'cashflow': 'failed'}, immutable=True)
        record_thesis_risk(self.store, plan,
            {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-fact'], 'rationale': 'persisted fact'},
            {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-fact'], 'issues': []}, now=NOW)
        self.store.put('plan', plan['id'], dict(plan, status='invalidated'))
        self.market.snapshots = lambda symbols, now=None: [quote(now, symbol=s) for s in symbols]
        self.runner.calendar = FixtureCalendar()

    def test_current_sealed_holding_risk_reassessed_once_on_enable_without_replaying_audit(self):
        self.held_risk()
        self.runner.run_once()
        old_ids = {key for key, report in self.store.items('report') if report['kind'] == '风险提示'}
        self.assertTrue(old_ids)
        self.assertEqual(self.sent, [])
        self.config.notifications_enabled = True
        self.runner.run_once()
        notices = [(key, text) for key, text in self.sent if text.startswith('【持仓风险')]
        self.assertEqual(len(notices), 1)
        self.assertNotIn(notices[0][0], old_ids)
        self.assertIn('2.00股', notices[0][1])
        self.runner.run_once()
        self.assertEqual(len([text for _, text in self.sent if text.startswith('【持仓风险')]), 1)
        self.assertTrue(all(self.store.get('notification', key)['status'] == 'suppressed' for key in old_ids))

    def test_resolved_holding_risk_during_mute_is_not_replayed_on_enable(self):
        self.held_risk()
        self.runner.run_once()
        stage_entry(self.store, {'id': 'sold', 'type': 'sell', 'account': 'real', 'currency': 'USD',
            'timestamp': NOW, 'symbol': 'SOXX', 'quantity': '2', 'price': '100', 'fee': '0'})
        confirm_entry(self.store, 'sold')
        self.config.notifications_enabled = True
        self.runner.run_once()
        self.assertFalse(any(text.startswith('【持仓风险') for _, text in self.sent))

    def test_enable_does_not_bypass_prior_unknown_risk_notification(self):
        self.held_risk()
        self.config.notifications_enabled = True
        self.runner.run_once()
        old_id = next(key for key, report in self.store.items('report') if report['kind'] == '风险提示')
        self.store.put('feishu_outbox', 'old-unknown', {'identity': 'notice:' + old_id, 'status': 'unknown'})
        self.sent.clear()
        self.config.notifications_enabled = False
        self.runner.run_once()
        self.config.notifications_enabled = True
        self.runner.run_once()
        self.assertFalse(any(text.startswith('【持仓风险') for _, text in self.sent))

    def test_same_incident_muted_after_live_alert_resets_operations_baseline(self):
        self.config.notifications_enabled = True
        self.store.put('research_worker', 'latest', {'status': 'blocked', 'errors': [{'error': 'UnknownModelOutcome'}]})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 1)
        self.config.notifications_enabled = False
        self.runner._observe_health({'errors': []}, NOW)
        self.assertFalse(self.store.get('ops_health', 'latest')['notice_attempted'])
        self.config.notifications_enabled = True
        self.store.put('research_worker', 'latest', {'status': 'completed', 'errors': []})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 1)

    def test_same_business_impact_different_stage_error_and_budget_state_does_not_spam(self):
        self.config.notifications_enabled = True
        self.store.put('research_worker', 'latest', {'status': 'blocked', 'errors': [{'error': 'ValueError'}]})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 0)
        self.store.put('research_worker', 'latest', {'status': 'completed', 'errors': []})
        self.runner._observe_health({'errors': [{'stage': 'archive_ingestion', 'error': 'ModelOutputInvalid'}]}, NOW)
        self.store.put('research_worker', 'latest', {'status': 'deferred', 'errors': []})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.store.get('ops_health', 'latest')['status'], 'deferred')
        self.store.put('research_worker', 'latest', {'status': 'needs_review', 'errors': []})
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 0)  # Internal research state changes stay quiet.
        for _ in range(3):
            self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 0)  # needs_review itself is not a fault.


if __name__ == '__main__':
    unittest.main()
