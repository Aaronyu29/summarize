"""Offline issuer-format fixtures; values/column layouts checked against Circle PDFs."""
import base64
import copy
import hashlib
import unittest

from rhino_invest.market import normalize_fundamentals, _primary_verified
from rhino_invest.primary_evidence import (
    enrich_primary_fundamentals, CIRCLE_IR, CIRCLE_Q2_2026, CIRCLE_FY2025)
from rhino_invest.store import Store
from tests_invest.test_primary_evidence_delivery import pdf, Response, Transport


NOW = '2026-10-24T03:00:00Z'
QUARTER = '''CIRCLE REPORTS SECOND QUARTER 2026 RESULTS
NEW YORK - AUGUST 5, 2026 - Circle Internet Group, Inc. (NYSE: CRCL)
CONSOLIDATED STATEMENTS OF OPERATIONS (unaudited)
(in $ thousands, except per share information)
Three Months Ended Six Months Ended
June 30, 2026 June 30, 2025 June 30, 2026 June 30, 2025
Revenue and reserve income
Reserve income $ 667,733 $ 634,274 $ 1,320,241 $ 1,192,185
Total revenue and reserve income 701,315 658,078 1,395,448 1,236,651
Operating income (loss) from continuing operations 34,359 (325,582) 79,361 (232,642)
Net income (loss) from continuing operations 48,214 (482,100) 103,460 (417,309)
Earnings (loss) per share attributable to common stockholders:
Basic $ 0.19 $ (4.48) $ 0.42 $ (5.04)
Diluted $ 0.18 $ (4.48) $ 0.39 $ (5.04)
Weighted-average common shares used in computing earnings
Non-GAAP reconciliation (in $ thousands)
Adjusted EBITDA (New Definition) $ 143,478 $ 151,401 $ 175,910 $ 171,476 $ 132,997
'''
ANNUAL = '''CIRCLE REPORTS FOURTH QUARTER AND FULL FISCAL YEAR 2025 RESULTS
NEW YORK - FEBRUARY 25, 2026 - Circle Internet Group, Inc. (NYSE: CRCL)
CONSOLIDATED STATEMENTS OF OPERATIONS
(in thousands, except per share information)
Three Months Ended Year Ended
December 31, 2025 December 31, 2024 December 31, 2025 December 31, 2024
Revenue and reserve income
Reserve income $ 733,396 $ 432,967 $ 2,636,822 $ 1,661,084
Total revenue and reserve income 770,232 435,367 2,746,642 1,676,253
Operating income (loss) from continuing operations 55,187 794 (96,435) 167,158
Net income (loss) from continuing operations 133,406 4,433 (69,518) 156,991
Earnings (loss) per share attributable to common stockholders:
Diluted earnings (loss) per common share attributable to common stockholders:
Continuing operations $ 0.43 $ 0.00 $ (0.44) $ 0.30
Diluted earnings (loss) per common share attributable to common stockholders $ 0.43 $ 0.00 $ (0.44) $ 0.30
Weighted-average shares used in computing earnings
Non-GAAP reconciliation (in $ thousands)
Year Ended December 31, 2025 December 31, 2024
Adjusted EBITDA $ 582,215 $ 284,871
'''


def sources():
    return {
        CIRCLE_IR: Response('<html><h1>Circle Investor Relations</h1><a href="' + CIRCLE_Q2_2026 +
            '">Earnings Press Release of Q2 2026, PDF file</a></html>', kind='text/html'),
        CIRCLE_Q2_2026: Response(pdf(QUARTER), kind='application/pdf'),
        CIRCLE_FY2025: Response(pdf(ANNUAL), kind='application/pdf')}


class CirclePrimaryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def enrich(self, data=None, docs=None, now=NOW):
        native = data or {'symbol': 'CRCL', 'source': 'longbridge-openapi',
            'identity': [{'symbol': 'CRCL.US', 'currency': 'USD'}],
            'company_profile': {'company_name': 'Circle Internet Group, Inc.'}}
        transport = Transport(docs if docs is not None else sources())
        result = enrich_primary_fundamentals(self.store, 'CRCL', native, now=now, transport=transport)
        return normalize_fundamentals(result, store=self.store), transport

    def test_reported_quarter_annual_loss_and_non_gaap_are_separate_not_annualized(self):
        result, transport = self.enrich()
        primary = result['primary_financials']
        eps = {(v['period_type'], v['fiscal_period']): v for v in primary['facts'] if v['metric'] == 'eps_diluted'}
        self.assertEqual(eps['quarterly', 'Q2']['value'], '0.18')
        self.assertEqual(eps['annual', 'FY']['value'], '-0.44')
        self.assertEqual(eps['quarterly', 'Q4']['value'], '0.43')
        self.assertEqual(eps['quarterly', 'Q2']['published_at'], '2026-08-05T23:59:59+00:00')
        self.assertEqual(eps['quarterly', 'Q2']['price_unit'], 'per_share')
        self.assertFalse(primary['annual_forecast_verified'])
        self.assertNotIn('0.72', {v['value'] for v in primary['facts']})
        self.assertNotIn('0.39', {v['value'] for v in primary['facts']})  # Six-month EPS is not a quarter/year.
        self.assertEqual(len(primary['facts']), 15)
        self.assertTrue(all(v['accounting_basis'] == 'GAAP' and v['source_verified'] for v in primary['facts']))
        adjusted = primary['non_gaap_facts']
        self.assertEqual([v['value'] for v in adjusted], ['143478000', '582215000'])
        self.assertTrue(all(v['metric'] == 'adjusted_ebitda' and v['accounting_basis'] == 'non_GAAP' for v in adjusted))
        self.assertEqual(result['instrument_contract']['quote_price_unit'], 'per_share')
        self.assertEqual(result['primary_gaps'], [])
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.store.events('ledger'), [])
        self.assertEqual(self.store.items('plan'), [])

    def test_original_bytes_text_dates_refs_and_native_provider_fields_are_preserved(self):
        native = {'symbol': 'CRCL', 'financial_report': {'eps': 'native'},
            'consensus': {'source': 'native', 'estimate': 'unverified'},
            'reference_quote': {'price': '84.92', 'status': 'historical'}, 'fetched_at': NOW}
        result, _ = self.enrich(native)
        for key in native:
            self.assertEqual(result[key], native[key])
        for _, evidence in self.store.items('external_evidence'):
            self.assertEqual(hashlib.sha256(evidence['content'].encode()).hexdigest(), evidence['sha256'])
            source = self.store.get('primary_source', evidence['source_id'])
            self.assertEqual(hashlib.sha256(base64.b64decode(source['raw_base64'])).hexdigest(), evidence['raw_sha256'])
            self.assertEqual(evidence['observed_at'], '2026-10-24T03:00:00+00:00')
            self.assertNotEqual(evidence['published_at'], evidence['observed_at'])

    def test_future_release_is_archived_but_cannot_become_q3_reported_facts(self):
        docs = sources()
        future_url = 'https://s206.q4cdn.com/265218871/files/doc_news/2026/Nov/04/Q3.pdf'
        docs[CIRCLE_IR] = Response('<html><p>Circle Internet Group Investor Relations latest quarterly earnings releases</p><a href="' + future_url +
            '">Earnings Press Release of Q3 2026</a></html>', kind='text/html')
        docs[future_url] = Response(pdf(QUARTER.replace('AUGUST 5', 'NOVEMBER 4')), kind='application/pdf')
        result, _ = self.enrich(docs=docs)
        self.assertTrue(any(g['code'] == 'SOURCE_NOT_AVAILABLE_AS_OF' for g in result['primary_gaps']))
        self.assertFalse(any(v['fiscal_period'] == 'Q3' for v in result['primary_financials']['facts']))
        self.assertEqual(result['primary_financials']['latest_reported_period_end'], '2026-06-30')
        self.assertTrue(any(v['source_url'] == future_url for _, v in self.store.items('primary_source')))

    def test_wrong_issuer_unit_and_column_order_fail_without_fabricating_eps(self):
        for text in (QUARTER.replace('NYSE: CRCL', 'NYSE: OTHER'),
                     QUARTER.replace('thousands, except per share', 'millions, except per share'),
                     QUARTER.replace('June 30, 2026 June 30, 2025', 'June 30, 2025 June 30, 2026'),
                     QUARTER.replace('Diluted $ 0.18 $ (4.48) $ 0.39 $ (5.04)', 'Diluted $ 0.18')):
            with self.subTest(text=text[:50]), Store(':memory:') as store:
                docs = sources()
                docs[CIRCLE_Q2_2026] = Response(pdf(text), kind='application/pdf')
                result = enrich_primary_fundamentals(store, 'CRCL', {}, now=NOW, transport=Transport(docs))
                self.assertTrue(any(g['field'] == 'CRCL_2026Q2' and g['blocking'] for g in result['primary_gaps']))
                self.assertFalse(any(v['fiscal_period'] == 'Q2' for v in result['primary_financials']['facts']))

    def test_transient_ir_discovery_failure_changes_no_material_fact_hash(self):
        healthy, _ = self.enrich()
        docs = sources()
        docs[CIRCLE_IR] = Response(status=403)
        failed, _ = self.enrich(docs=docs, now='2026-10-25T03:00:00Z')
        self.assertEqual(healthy['primary_evidence_hash'], failed['primary_evidence_hash'])
        self.assertEqual(failed['primary_collection']['status'], 'dated_historical_fallback')
        self.assertEqual(failed['primary_collection']['gaps'][0]['code'], 'SOURCE_HTTP_403')
        self.assertEqual(failed['primary_refresh_after'], '2026-10-25T09:00:00+00:00')

    def test_partial_collection_failure_keeps_verified_newer_package_and_original_dates(self):
        healthy, _ = self.enrich()
        self.store.put('fundamental_cache', 'CRCL', {'data': healthy})
        docs = sources()
        docs[CIRCLE_Q2_2026] = Response(status=403)
        failed, _ = self.enrich(docs=docs, now='2026-10-25T03:00:00Z')
        self.assertEqual(failed['primary_evidence_hash'], healthy['primary_evidence_hash'])
        self.assertEqual(failed['primary_evidence'], healthy['primary_evidence'])
        self.assertEqual(failed['primary_collection']['status'], 'retained_last_known_evidence')
        self.assertEqual(failed['primary_collection']['gaps'][0]['code'], 'SOURCE_HTTP_403')
        self.assertEqual(failed['primary_financials']['latest_reported_period_end'], '2026-06-30')

    def test_cdn_scope_cannot_verify_other_company_or_tampered_content(self):
        result, _ = self.enrich()
        fact = result['primary_financials']['facts'][0]
        evidence = self.store.get('external_evidence', fact['evidence_refs'][0])
        with Store(':memory:') as store:
            wrong = copy.deepcopy(evidence)
            wrong['source_url'] = wrong['source_url'].replace('265218871', '999999999')
            store.put('external_evidence', wrong['id'], wrong)
            self.assertFalse(_primary_verified(store, fact))
            wrong['source_url'] = evidence['source_url']
            wrong['content'] += ' altered'
            store.put('external_evidence', wrong['id'], wrong)
            self.assertFalse(_primary_verified(store, fact))


if __name__ == '__main__':
    unittest.main()
