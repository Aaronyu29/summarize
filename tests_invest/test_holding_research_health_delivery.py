"""Holding-only deferred research must not pass cloud readiness as healthy."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rhino_invest.runner import Runner
from rhino_invest.store import Store

NOW = '2026-10-24T08:00:00+00:00'


class HoldingResearchHealthTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.runner = Runner.__new__(Runner)
        self.runner.store = self.store
        self.runner.config = SimpleNamespace(archive_database=None, notifications_enabled=False)
        self.runner.model = SimpleNamespace(config={'enabled': True}, run=Mock(side_effect=AssertionError('No paid call')))
        self.runner.execute = True
        self.runner.clock = lambda: NOW
        self.runner._stop = threading.Event()
        self.runner._plans = lambda: []
        self.runner.feishu = SimpleNamespace(notify=Mock(side_effect=AssertionError('No notification')))

    def test_no_archive_holding_risk_defer_persists_nonhealthy_summary(self):
        result = {'symbols': ['CRCL'], 'plans': [], 'errors': [],
                  'deferred_unresolved_risk': [{'code': 'UNRESOLVED_VERIFIED_THESIS_RISK'}]}
        with patch('rhino_invest.runner.ingest_holdings', return_value=result):
            self.runner._research_cycle(['CRCL'], {})
        summary = self.store.get('research_worker', 'latest')
        self.assertEqual(summary['status'], 'deferred')
        self.assertEqual(summary['error_count'], 0)
        self.assertEqual(summary['deferred_count'], 1)
        self.assertEqual(summary['holding_research']['deferred_unresolved_risk_count'], 1)
        self.assertEqual(summary['deferred'][0]['reason'], 'UNRESOLVED_VERIFIED_THESIS_RISK')
        # The restore/ready gate requires completed AND empty deferred: neither
        # condition may be reused from an earlier healthy worker snapshot.
        self.assertFalse(summary['status'] == 'completed' and not summary['deferred'])
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(self.store.get('ops_health', 'latest')['status'], 'deferred')
        self.runner.model.run.assert_not_called()
        self.runner.feishu.notify.assert_not_called()

    def test_synchronous_holding_defer_changes_healthy_baseline_without_alarm_when_muted(self):
        self.store.put('ops_health', 'latest', {'status': 'healthy', 'signature': 'old-healthy',
                       'sequence': 1, 'notice_attempted': False})
        self.runner._observe_health({'errors': [], 'holding_research': {
            'deferred_unresolved_risk': [{'code': 'UNRESOLVED_VERIFIED_THESIS_RISK'}]}}, NOW)
        self.assertEqual(self.store.get('ops_health', 'latest')['status'], 'deferred')
        self.assertFalse(self.store.get('ops_health', 'latest')['notice_attempted'])
        self.assertEqual(self.store.items('ops_notice')[0][1]['status'], 'suppressed')
        self.runner.feishu.notify.assert_not_called()

    def test_holdings_needs_review_without_verified_risk_is_not_operational_failure(self):
        result = {'symbols': ['CRCL'], 'plans': [{'status': 'needs_review'}],
                  'errors': [], 'deferred_unresolved_risk': []}
        with patch('rhino_invest.runner.ingest_holdings', return_value=result):
            self.runner._research_cycle(['CRCL'], {})
        summary = self.store.get('research_worker', 'latest')
        self.assertEqual(summary['status'], 'completed')
        self.assertEqual(summary['holding_research']['needs_evidence_count'], 1)
        self.assertEqual(summary['holding_research']['deferred_unresolved_risk_count'], 0)
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(self.store.get('ops_health', 'latest')['status'], 'healthy')
        self.runner.feishu.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
