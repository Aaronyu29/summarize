"""Offline worker acceptance for primary-source refresh and material-change wiring."""
import copy
import unittest
from datetime import timedelta
from unittest.mock import patch

from rhino_invest.common import parse_time
from rhino_invest.config import Config
from rhino_invest.runner import Runner
from rhino_invest.store import Store
from tests_invest.test_runner_delivery import Market, Feishu, Calendar


class EvidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.now = '2026-10-24T01:00:00Z'
        self.market = Market()
        self.native_calls, self.primary_calls = [], []
        self.value = '10.65'
        self.failed = False
        def native(symbol, now=None):
            self.native_calls.append(now)
            return {'symbol': symbol, 'source': 'longbridge-openapi', 'fetched_at': now,
                'financial_report': {'reported_eps': '10.5478'},
                'financial_report_request': {'kind': 'All', 'period': 'Annual'}}
        self.market.fundamentals = native
        def enrich(store, symbol, data, now=None):
            self.primary_calls.append(now)
            data = copy.deepcopy(data)
            data.update(primary_evidence_hash='fact:' + self.value,
                primary_financials={'symbol': symbol, 'facts': [{'metric': 'eps_diluted', 'value': self.value,
                    'currency': 'USD', 'price_unit': 'per_adr', 'period_type': 'annual', 'fiscal_year': 2025,
                    'period_end': '2025-12-31', 'source_excerpt': 'official diluted ADR earnings', 'evidence_refs': []}],
                    'instrument': {}}, primary_gaps=[],
                primary_evidence=[{'id': 'unchanged-source', 'observed_at': now, 'sha256': '1' * 64}],
                primary_refresh_after=(parse_time(now) + timedelta(hours=6 if self.failed else 24)).isoformat())
            if self.failed:
                data.update(primary_financials={'symbol': symbol, 'facts': [], 'instrument': {}},
                    primary_evidence=[], primary_gaps=[{'code': 'PRIMARY_SOURCE_UNAVAILABLE', 'blocking': True}],
                    primary_evidence_hash='no-current-primary-source')
            return data
        self.runner = Runner(self.store, Config(symbols=('TSM',), notifications_enabled=False),
            market=self.market, feishu=Feishu(), calendar=Calendar(), primary_enricher=enrich,
            execute=True, clock=lambda: self.now)

    def run_worker(self):
        with patch('socket.socket', side_effect=AssertionError('offline test must not use network')):
            result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        return result

    def test_legacy_same_period_cache_is_enriched_once_then_reused(self):
        self.store.put('fundamental_cache', 'TSM', {'period': '2026-10-23',
            'data': {'symbol': 'TSM', 'financial_report': {'reported_eps': '10.5478'}}})
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 1)
        self.assertEqual(self.store.get('fundamental_cache', 'TSM')['enrichment_revision'], 'issuer-evidence-v1')
        self.now = '2026-10-24T02:00:00Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 1)
        self.assertEqual(len(self.native_calls), 1)

    def test_same_period_transient_gap_retries_when_due_then_new_fact_queues_only_once(self):
        self.failed = True
        self.run_worker()
        self.now = '2026-10-24T06:59:59Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 1)
        self.failed = False
        self.now = '2026-10-24T07:00:00Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 2)
        changes = [(key, value) for key, value in self.store.items('review_trigger') if value['kind'] == 'fundamental_change']
        self.assertEqual(len(changes), 1)
        self.now = '2026-10-24T08:00:00Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 2)
        self.assertEqual(len(self.store.items('review_trigger')), 1)
        self.assertEqual(self.store.items('model_call'), [])

    def test_refresh_observation_and_request_metadata_do_not_manufacture_paid_review_event(self):
        self.run_worker()
        cached = self.store.get('fundamental_cache', 'TSM')
        cached['data'].pop('financial_report_request', None)  # Prior release did not store this metadata.
        cached['data']['primary_refresh_after'] = self.now
        self.store.put('fundamental_cache', 'TSM', cached)
        self.now = '2026-10-24T02:00:00Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 2)
        self.assertEqual(self.store.items('review_trigger'), [])

    def test_temporary_primary_fetch_failure_does_not_manufacture_new_external_research_fact(self):
        self.run_worker()
        cached = self.store.get('fundamental_cache', 'TSM')
        cached['data']['primary_refresh_after'] = self.now
        self.store.put('fundamental_cache', 'TSM', cached)
        self.failed = True
        self.now = '2026-10-24T02:00:00Z'
        self.run_worker()
        self.assertEqual(len(self.primary_calls), 2)
        self.assertEqual(self.store.items('review_trigger'), [])

    def test_dry_run_never_fetches_primary_issuer_sources(self):
        self.runner.execute = False
        self.run_worker()
        self.assertEqual(self.primary_calls, [])


if __name__ == '__main__':
    unittest.main()
