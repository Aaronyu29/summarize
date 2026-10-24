"""Offline research contracts: evidence availability is separate from execution."""
import copy
import hashlib
from decimal import Decimal
import unittest

from rhino_invest.common import stable_id
from rhino_invest.ledger import stage_entry, confirm_entry
from rhino_invest.research import (ingest_archives, review_plan, bind_research_supplement,
    _etf_input_gaps, _validate_etf_target, _stable_fundamentals, _primary_text_transport, _run)
from tests_invest import test_research as research_tests
from tests_invest.test_research import FixtureModel, NOW


class IndependentModel(FixtureModel):
    def __init__(self, symbol='NVDA', reject=False, uncertain=False):
        super().__init__(uncertain=uncertain)
        self.symbol, self.reject, self.payloads = symbol, reject, []

    def run(self, purpose, payload, **kwargs):
        self.payloads.append((purpose, copy.deepcopy(payload)))
        result = super().run(purpose, payload, **kwargs)
        if purpose == 'extract_levels':
            result['levels'][0]['symbol'] = self.symbol
        if purpose == 'compile_plan':
            plan = result['plan']
            plan['entry'].update(basis='independent_valuation', source_level_ids=[],
                conditions=['Price within independently valued band'])
            if self.symbol == 'SOXX':
                for target, growth in zip(plan['targets'], ('0.1', '-0.1')):
                    baseline = payload['fundamentals']['etf_valuation']
                    nav, pe = baseline['nav']['value'], baseline['pe']['value']
                    target.update(method='etf_nav_scenario',
                        price=str(Decimal(nav) * (1 + Decimal(growth)) ** 2),
                        assumptions={'nav_per_share': nav, 'current_pe': pe, 'annual_earnings_growth': growth,
                            'exit_pe': pe, 'years': '2', 'fees_treatment': 'Growth is net of the disclosed annual expense ratio.',
                            'rationale': 'Explicit net earnings growth scenario with concentration and cyclicality risk.',
                            'currency': 'USD', 'price_unit': 'per_share', 'evidence_refs': payload['evidence_ids']})
        if purpose == 'review_plan' and self.reject:
            result.update(accepted=False, issues=[{'code': 'EPS_UNIT_UNVERIFIED', 'scope': 'earnings_unit',
                'detail': 'Need an additional primary denominator explanation', 'required_evidence': ['issuer footnote']}])
        return result


class EvidenceContractTests(unittest.TestCase):
    setUp = research_tests.ResearchTests.setUp

    def primary(self, symbol='NVDA', *, etf=False):
        excerpt = 'Diluted earnings per common share are stated in USD.'
        action_excerpt = 'A forward split was announced on 2026-08-21 and becomes effective on 2026-11-05.'
        text = excerpt + '\nOriginal issuer financial table, NAV, holdings, fees.\n' + action_excerpt
        ref = stable_id('test-primary', symbol, text)
        self.store.put('external_evidence', ref, {'id': ref, 'source_url': 'https://www.ishares.com/us/test' if etf else 'https://www.sec.gov/test',
            'content': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'as_of': '2026-10-22'}, immutable=True)
        unit = {'quote_price_unit': 'per_share', 'quote_currency': 'USD', 'is_adr': False,
            'stock_eps_valuation_applicable': not etf, 'source_excerpt': excerpt, 'evidence_refs': [ref]}
        data = {'symbol': symbol, 'source': 'longbridge-openapi', 'fetched_at': NOW,
            'identity': [{'symbol': symbol + '.US', 'currency': 'USD',
                'name_en': 'iShares Semiconductor ETF' if etf else 'Test issuer'}],
            'primary_evidence': [{'id': ref, 'sha256': hashlib.sha256(text.encode()).hexdigest()}],
            'primary_financials': {'symbol': symbol, 'instrument': unit, 'facts': [{'metric': 'eps_diluted',
                'value': '4', 'currency': 'USD', 'price_unit': 'per_share', 'period_type': 'annual',
                'fiscal_year': 2025, 'period_end': '2025-12-31', 'value_kind': 'reported',
                'source_excerpt': excerpt, 'evidence_refs': [ref]}]}}
        if etf:
            common = {'as_of': '2026-10-22', 'evidence_refs': [ref], 'source_excerpt': excerpt}
            data['etf_valuation'] = {'issuer_verified': True,
                'nav': dict(common, value='100', currency='USD', price_unit='per_share'),
                'pe': dict(common, value='20', methodology='Issuer portfolio PE'),
                'holdings': dict(common, rows=[{'symbol': 'NVDA', 'weight': '1'}], coverage='1', concentration={'top10': '1'}),
                'expense_ratio': dict(common, value='0.34', unit='percent_per_year'),
                'corporate_actions': [{'type': 'split', 'effective_at': '2026-11-05', 'announced_at': '2026-08-21',
                    'new_shares_per_old': None, 'evidence_refs': [ref], 'source_excerpt': action_excerpt}]}
        return data, ref

    def ingest(self, model, data, now=NOW):
        result = ingest_archives(self.store, self.db, model=model, fundamentals={model.symbol: data}, now=now)
        self.assertEqual(result['errors'], [])
        return result['plans'][0]

    def test_primary_bundle_both_reviews_independent_plan_without_unused_chart_or_cash(self):
        data, ref = self.primary()
        model = IndependentModel(uncertain=True)
        plan = self.ingest(model, data)
        self.assertEqual(plan['status'], 'active', plan['review_issues'])
        self.assertTrue(all(not row['verified'] for _, row in self.store.items('research_level')))
        self.assertEqual(plan['entry']['amount_usd'], '1000')
        self.assertNotIn('take_profit_position_basis', plan)
        for purpose, payload in model.payloads:
            if purpose not in {'compile_plan', 'review_plan'}:
                continue
            self.assertEqual(payload['levels'], [])
            self.assertFalse(payload['policy']['cash_verified'])
            self.assertTrue(payload['policy']['empty_position_basis_means_initial_plan'])
            bundle = payload['evidence_bundle']
            self.assertEqual(set(bundle['evidence_ids']), set(payload['evidence_ids']))
            raw = next(item for item in bundle['artifacts'] if item['id'] == ref)
            self.assertEqual(raw['content'], self.store.get('external_evidence', ref)['content'])
            self.assertEqual(raw['sha256'], hashlib.sha256(raw['content'].encode()).hexdigest())
            self.assertTrue(any(item.get('payload_path') == '/fundamentals' for item in bundle['artifacts']))

    def test_actual_cash_is_passed_without_promising_execution(self):
        data, _ = self.primary()
        stage_entry(self.store, {'id': 'opening', 'account': 'real', 'type': 'opening_cash', 'amount': '20000',
            'currency': 'USD', 'timestamp': NOW})
        confirm_entry(self.store, 'opening')
        model = IndependentModel()
        self.ingest(model, data)
        payload = next(value for purpose, value in model.payloads if purpose == 'compile_plan')
        self.assertTrue(payload['policy']['cash_verified'])
        self.assertEqual(payload['policy']['execution_accounts']['real']['cash'], '20000')

    def test_new_verified_supplement_rechecks_once_no_clock_or_quote_paid_loop(self):
        data, ref = self.primary()
        model = IndependentModel(reject=True)
        old = self.ingest(model, data)
        self.assertEqual(old['status'], 'needs_review')
        count = len(model.calls)
        changed = copy.deepcopy(data)
        changed.update(observed_at='later', collected_at='later', primary_refresh_after='later',
            reference_quote={'comparison_quote': {'price': '999', 'quote_time': 'later'}})
        self.ingest(model, changed, '2026-10-24T15:00:00+00:00')
        self.assertEqual(len(model.calls), count)
        record = bind_research_supplement(self.store, 'NVDA', {'denominator_explanation': 'see original source'},
            evidence_refs=[ref], verified_by='offline-test-review', verified_at=NOW)
        self.ingest(model, data)
        self.assertEqual(len(model.calls), count + 2)
        self.assertEqual(len(self.store.items('research_repair')), 1)
        replay = bind_research_supplement(self.store, 'NVDA', record['data'], evidence_refs=[ref],
            verified_by='offline-test-review', verified_at='2026-10-24T15:00:00+00:00')
        self.assertEqual(replay['id'], record['id'])
        self.ingest(model, data)
        self.assertEqual(len(model.calls), count + 2)
        self.assertTrue(self.store.items('research_gap'))

    def test_same_failed_initial_facts_event_deduplicates_but_active_or_held_review_does_not(self):
        data, _ = self.primary()
        model = IndependentModel(reject=True)
        plan = self.ingest(model, data)
        count = len(model.calls)
        result = review_plan(self.store, plan, model, event_id='new-report', evidence={'fundamentals': {'NVDA': data}}, now=NOW)
        self.assertTrue(result['deduplicated'])
        self.assertEqual(len(model.calls), count)
        self.store.put('reported_holding', 'note', {'symbol': 'NVDA', 'account': 'real', 'reported_at': NOW, 'market_value': '5'})
        review_plan(self.store, plan, model, event_id='held-report', evidence={'fundamentals': {'NVDA': data}}, now=NOW)
        self.assertEqual(model.calls[count:], ['event_review', 'compile_plan', 'review_plan'])

    def test_complete_etf_nav_scenario_is_independently_reviewed_and_split_deadline_sealed(self):
        data, _ = self.primary('SOXX', etf=True)
        model = IndependentModel('SOXX')
        plan = self.ingest(model, data)
        self.assertEqual(plan['status'], 'active', plan['review_issues'])
        self.assertEqual(plan['price_basis_valid_until'], '2026-11-05T05:00:00+00:00')
        self.assertEqual(plan['next_review_at'], plan['price_basis_valid_until'])
        self.assertEqual(plan['targets'][0]['price'], '121.00')
        self.assertEqual(plan['targets'][0]['method'], 'etf_nav_scenario')
        reviewed = next(payload for purpose, payload in model.payloads if purpose == 'review_plan')
        self.assertEqual(reviewed['plan']['price_basis_valid_until'], plan['price_basis_valid_until'])
        binding = self.store.get('research_plan_review', plan['review_evidence_id'])
        self.assertTrue(binding['accepted'])

    def test_etf_missing_stale_unaligned_fees_and_arithmetic_are_typed_not_accepted(self):
        data, ref = self.primary('SOXX', etf=True)
        bad = copy.deepcopy(data)
        bad['etf_valuation']['pe']['as_of'] = '2026-10-01'
        bad['etf_valuation']['expense_ratio']['evidence_refs'] = []
        gaps = _etf_input_gaps(bad, NOW, {ref}, self.store)
        codes = {gap['code'] for gap in gaps}
        self.assertTrue({'ETF_PE_DATE_INVALID', 'ETF_BASELINE_DATES_UNALIGNED', 'ETF_FEES_UNRESOLVED'} <= codes)
        plan = self.ingest(IndependentModel('SOXX'), data)
        target = copy.deepcopy(plan['targets'][0])
        target['price'] = '121.02'
        with self.assertRaisesRegex(ValueError, 'arithmetic mismatch'):
            _validate_etf_target(target, plan, data, set(plan['evidence_refs']))
        data['etf_valuation']['nav']['as_of'] = None
        blocked = self.ingest(IndependentModel('SOXX'), data)
        self.assertEqual(blocked['status'], 'needs_review')

    def test_rejected_review_keeps_typed_material_objection_and_clock_hash_is_stable(self):
        data, _ = self.primary()
        plan = self.ingest(IndependentModel(reject=True), data)
        self.assertEqual(plan['status'], 'needs_review')
        self.assertIn('EPS_UNIT_UNVERIFIED', {gap['code'] for gap in plan['research_gaps']})
        self.assertFalse(self.store.get('research_plan_review', plan['review_evidence_id'])['accepted'])
        changed = dict(data, primary_refresh_after='2030-01-01', observed_at='2030-01-01',
            primary_collection={'status': 'retained_last_known_evidence', 'attempted_at': '2030-01-01', 'gaps': ['fetch unavailable']})
        self.assertEqual(_stable_fundamentals(data), _stable_fundamentals(changed))

    def test_reported_broker_cash_nav_and_exact_tickers_reach_research_without_rearming(self):
        from rhino_invest.broker_snapshot import record_broker_snapshot
        data, _ = self.primary()
        raw = {'id': 'offline-broker', 'account': 'real', 'broker': 'IBKR', 'currency': 'USD',
            'recorded_at': NOW, 'as_of': None,
            'source': {'type': 'user_screenshot', 'reference': 'offline fixture'},
            'summary': {'cash': '23921.59', 'net_liquidation': '24536.28'},
            'holdings': [{'symbol': 'GOOG', 'market_value': '480.55'}, {'symbol': 'CRCL', 'market_value': '128.58'}],
            'supersedes_symbols': ['GOOGL']}
        record_broker_snapshot(self.store, raw)
        model = IndependentModel()
        plan = self.ingest(model, data)
        payload = next(value for purpose, value in model.payloads if purpose == 'compile_plan')
        account = payload['policy']['execution_accounts']['real']
        self.assertEqual((account['cash'], account['total_equity']), ('23921.59', '24536.28'))
        self.assertEqual({item['symbol'] for item in account['reported_holdings']}, {'GOOG', 'CRCL'})
        self.assertTrue(all(item.get('quantity') is None for item in account['reported_holdings']))
        self.assertFalse(account['cash_complete'])
        self.assertFalse(account['holdings_complete'])
        self.assertEqual(plan['status'], 'active')
        count = len(model.calls)
        raw.update(id='offline-broker-new', recorded_at='2026-10-24T15:00:00+00:00')
        raw['summary']['cash'] = '23922'
        record_broker_snapshot(self.store, raw)
        self.ingest(model, data, '2026-10-24T15:00:00+00:00')
        self.assertEqual(len(model.calls), count)

    def test_large_primary_projection_retains_all_claims_context_offsets_and_hashes(self):
        text = ('Old historical row\t1.0\n' * 15000
            + '\nHistorical\nAs Of\tNAV per Share\nOct 22, 2026\t475.335589\nOct 21, 2026\t510.397665\n')
        projected = _primary_text_transport(text, {'NAV per Share', '475.335589'})
        self.assertNotIn('content', projected)
        self.assertEqual(projected['original_text_char_count'], len(text))
        self.assertLess(len(str(projected)), 2000)
        for part in projected['excerpts']:
            self.assertEqual(part['content'], text[part['start_char']:part['end_char']])
            self.assertEqual(part['sha256'], hashlib.sha256(part['content'].encode()).hexdigest())
        included = '\n'.join(part['content'] for part in projected['excerpts'])
        self.assertIn('Oct 22, 2026\t475.335589', included)
        self.assertIn('As Of\tNAV per Share', included)
        fallback = _primary_text_transport(text, {'not in the original'})
        self.assertEqual(fallback['content'], text)

    def test_added_request_metadata_cannot_unlock_legacy_unknown(self):
        from rhino_invest.model import ModelGateway, UnknownModelOutcome
        ingest_archives(self.store, self.db, now=NOW)
        snapshot = self.store.items('research_evidence')[0][1]
        old = {'financial_report': {'period': '2025'}}
        scope = stable_id('research-logical-call', 'compile_plan', snapshot['id'], 'NVDA', old, None, [])
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        config = {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '.1', 'max_cost_usd_per_day': '2'}
        model = ModelGateway(self.store, config, transport)
        with self.assertRaises(UnknownModelOutcome):
            model.run('compile_plan', {'old': True}, call_scope=scope)
        new = dict(old, financial_report_request={'kind': 'All', 'period': 'Annual'},
            primary_collection={'status': 'retained_last_known_evidence', 'attempted_at': NOW, 'gaps': []})
        with self.assertRaises(UnknownModelOutcome):
            _run(model, 'compile_plan', {'symbol': 'NVDA', 'source': snapshot, 'fundamentals': new}, store=self.store)
        self.assertEqual(len(calls), 1)
        self.assertEqual(_stable_fundamentals(old), _stable_fundamentals(new))


if __name__ == '__main__':
    unittest.main()
