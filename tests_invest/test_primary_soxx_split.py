import copy
import unittest

from rhino_invest.market import _primary_verified
from rhino_invest.primary_evidence import enrich_primary_fundamentals, SOXX_SPLIT_PDF
from rhino_invest.research import _price_basis_deadline, _etf_input_gaps
from rhino_invest.store import Store
from tests_invest.test_primary_evidence_delivery import (
    soxx_sources, Response, Transport, pdf, SPLIT_SUPPLEMENT)


NOW = '2026-10-24T02:00:00Z'


class IssuerSplitSupplementTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def collect(self, docs=None, now=NOW):
        return enrich_primary_fundamentals(self.store, 'SOXX', {}, now=now,
            transport=Transport(docs if docs is not None else soxx_sources()))

    def test_official_ratio_record_distribution_and_trading_dates_never_apply_ledger_split(self):
        data = self.collect()
        action = data['etf_valuation']['corporate_actions'][0]
        self.assertEqual((action['new_shares_per_old'], action['ratio']), ('3', '3:1'))
        self.assertEqual(action['record_date'], '2026-11-03')
        self.assertEqual(action['distribution_date'], '2026-11-04')
        self.assertEqual(action['effective_at'], '2026-11-05')
        self.assertEqual(action['announced_at'], '2026-08-21')
        self.assertEqual(action['source_published_at'], '2026-10-23T23:59:59+00:00')
        self.assertEqual(action['status'], 'announced_not_effective')
        self.assertFalse(action['automatic_price_adjustment'])
        self.assertTrue(action['ledger_confirmation_required'])
        self.assertTrue(_primary_verified(self.store, action))
        self.assertEqual(self.store.events('ledger'), [])
        self.assertEqual(self.store.items('ledger_staged'), [])
        refs = set(action['evidence_refs'])
        self.assertEqual(_price_basis_deadline(data, NOW, refs, self.store), '2026-11-04T21:00:00+00:00')

    def test_future_confirming_page_is_not_backdated_to_old_announcement(self):
        data = self.collect(now='2026-10-20T23:59:59Z')
        self.assertIsNone(data['etf_valuation']['corporate_actions'][0]['new_shares_per_old'])
        self.assertEqual(data['primary_collection']['gaps'][0]['code'], 'SOURCE_NOT_AVAILABLE_AS_OF')
        self.assertFalse(any(v['source_url'] == SOXX_SPLIT_PDF for v in data['primary_evidence']))

    def test_conflicting_latest_ratio_or_dates_do_not_certify_known_split(self):
        for replacement in (SPLIT_SUPPLEMENT.replace('three-for-one', 'four-for-one'),
                            SPLIT_SUPPLEMENT.replace('distribution date will be\nNovember 4', 'distribution date will be\nNovember 2')):
            with self.subTest(replacement=replacement[:60]), Store(':memory:') as store:
                docs = soxx_sources()
                docs[SOXX_SPLIT_PDF] = Response(pdf(replacement), kind='application/pdf')
                data = enrich_primary_fundamentals(store, 'SOXX', {}, now=NOW, transport=Transport(docs))
                self.assertIsNone(data['etf_valuation']['corporate_actions'][0]['new_shares_per_old'])
                self.assertTrue(data['etf_valuation']['corporate_action_conflict'])
                self.assertTrue(any(g['blocking'] for g in data['primary_gaps']))
                self.assertEqual(store.events('ledger'), [])

    def test_failed_refresh_retains_previous_hash_bound_ratio_and_original_evidence(self):
        healthy = self.collect()
        self.store.put('fundamental_cache', 'SOXX', {'data': healthy})
        docs = soxx_sources()
        docs[SOXX_SPLIT_PDF] = Response(status=403)
        retried = self.collect(docs=docs, now='2026-10-25T02:00:00Z')
        self.assertEqual(retried['etf_valuation']['corporate_actions'], healthy['etf_valuation']['corporate_actions'])
        self.assertEqual(retried['primary_evidence_hash'], healthy['primary_evidence_hash'])
        self.assertEqual(retried['primary_collection']['status'], 'split_supplement_retained')

    def test_new_issuer_conflict_with_healthy_cached_ratio_is_material_and_never_masked_by_cache(self):
        for text, code in (
            (SPLIT_SUPPLEMENT.replace('three-for-one', 'four-for-one'), 'SOURCE_SPLIT_SUPPLEMENTS_CONFLICT'),
            (SPLIT_SUPPLEMENT.replace('distribution date will be\nNovember 4', 'distribution date will be\nNovember 2'), 'SOURCE_SPLIT_DATE_CONFLICT'),
            (SPLIT_SUPPLEMENT.replace('November 5, 2026', 'November 6, 2026'), 'SOURCE_SPLIT_ANNOUNCEMENT_CONFLICT')):
            with self.subTest(code=code), Store(':memory:') as store:
                healthy = enrich_primary_fundamentals(store, 'SOXX', {}, now=NOW, transport=Transport(soxx_sources()))
                original = copy.deepcopy(healthy)
                store.put('fundamental_cache', 'SOXX', {'data': healthy})
                docs = soxx_sources()
                docs[SOXX_SPLIT_PDF] = Response(pdf(text), kind='application/pdf')
                conflicting = enrich_primary_fundamentals(store, 'SOXX', {}, now='2026-10-25T02:00:00Z', transport=Transport(docs))
                etf = conflicting['etf_valuation']
                self.assertTrue(etf['corporate_action_conflict'])
                self.assertTrue(etf['issuer_verified'])  # NAV/PE/holdings remain known; avoid whole-package fallback.
                self.assertTrue(any(g['code'] == code and g['blocking'] for g in conflicting['primary_gaps']))
                self.assertNotEqual(conflicting['primary_evidence_hash'], healthy['primary_evidence_hash'])
                self.assertFalse(any(a.get('new_shares_per_old') == '3' for a in etf['corporate_actions']))
                ref = etf['corporate_action_conflict_evidence_refs'][0]
                evidence = store.get('external_evidence', ref)
                self.assertTrue(evidence)
                self.assertTrue(any(row['id'] == ref for row in conflicting['primary_evidence']))
                gaps = _etf_input_gaps(conflicting, '2026-10-25T02:00:00Z',
                    {row['id'] for row in conflicting['primary_evidence']}, store)
                self.assertTrue(any(g['code'] == 'ETF_CORPORATE_ACTION_CONFLICT' for g in gaps))
                source = store.get('primary_source', evidence['source_id'])
                self.assertIn('Supplement dated', source['content'])
                self.assertEqual(store.get('fundamental_cache', 'SOXX')['data'], original)
                self.assertEqual(store.events('ledger'), [])

    def test_past_trading_day_requires_unit_check_without_automatic_conversion(self):
        data = self.collect(now='2026-11-06T02:00:00Z')
        action = data['etf_valuation']['corporate_actions'][0]
        self.assertEqual(action['status'], 'effective_requires_unit_check')
        self.assertFalse(action['automatic_price_adjustment'])
        self.assertTrue(action['ledger_confirmation_required'])
        self.assertEqual(self.store.events('ledger'), [])


if __name__ == '__main__':
    unittest.main()
