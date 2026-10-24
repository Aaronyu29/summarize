import copy
import hashlib
import socket
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from rhino_invest.common import stable_id
from rhino_invest.market import MarketData, normalize_fundamentals
from rhino_invest.store import Store


NOW = '2026-10-24T01:00:00+00:00'


def financials():
    return {'symbol': 'TSM', 'source': 'longbridge-openapi', 'fetched_at': NOW,
        'identity': [{'symbol': 'TSM.US', 'currency': 'USD', 'name_en': 'Taiwan Semiconductor'}],
        'company_profile': {'name': 'Taiwan Semiconductor', 'company_name': 'TSMC', 'ads_ratio': '1:5'},
        'financial_report_request': {'kind': 'All', 'period': 'Annual'},
        'financial_report': {'list': {'IS': {'indicators': [{
            'currency': 'USD', 'accounts': [{'field': 'EPS', 'name': 'Earning Per Share(USD)',
                'tip': 'provider denominator not specified', 'percent': False,
                'values': [{'fp_end': '1767157200', 'period': 'FY 2025', 'year': 2025,
                    'value': '10.5478', 'yoy': '52.6'}]}]}]}}}}


def consensus(quarters, *, released=(), year=2027, metric='eps'):
    return {'currency': 'USD', 'current_index': 3, 'current_period': 'qf',
        'opt_periods': ['qf', 'saf', 'af'], 'list': [
            {'fiscal_year': year, 'fiscal_period': str(q), 'period_text': f'Q{q} {year}',
             'details': [{'key': metric, 'name': 'EPS(GAAP)', 'description': 'Diluted common stock EPS',
                'actual': '2.0' if q in released else None, 'estimate': '1.5',
                'is_released': q in released, 'comp': 'beat_est'}]} for q in quarters]}


class FundamentalNormalizationTests(unittest.TestCase):
    def test_native_financial_paths_currency_period_and_unknown_denominator(self):
        raw = financials()
        before = copy.deepcopy(raw)
        result = normalize_fundamentals(raw)
        self.assertEqual(raw, before)
        self.assertEqual(result['financial_report'], raw['financial_report'])
        fact = result['normalized_financials']['actuals'][0]
        self.assertEqual((fact['currency'], fact['period_type'], fact['fiscal_year'], fact['fiscal_period']),
            ('USD', 'annual', 2025, 'FY'))
        self.assertEqual(fact['period_end'], '2025-12-31')
        self.assertIsNone(fact['period_start'])
        self.assertIsNone(fact['published_at'])
        self.assertIsNone(fact['price_unit'])
        self.assertEqual(fact['value'], '10.5478')
        self.assertEqual(fact['raw_path'], '/financial_report/list/IS/indicators/0/accounts/0/values/0')
        value = raw['financial_report']['list']['IS']['indicators'][0]['accounts'][0]['values'][0]
        self.assertEqual(fact['raw_hash'], stable_id(value))
        self.assertIsNone(result['instrument_contract']['quote_price_unit'])
        self.assertIsNone(result['instrument_contract']['stock_eps_valuation_applicable'])

    def test_quarterly_report_label_is_never_relabelled_annual_by_request(self):
        raw = financials()
        row = raw['financial_report']['list']['IS']['indicators'][0]['accounts'][0]['values'][0]
        row.update(period='Q3 2026', year=2026, fp_end='1790740800')
        fact = normalize_fundamentals(raw)['normalized_financials']['actuals'][0]
        self.assertEqual((fact['period_type'], fact['fiscal_period']), ('quarterly', 'Q3'))
        self.assertEqual(fact['period_end'], '2026-09-30')
        row['period'] = 'year to date'
        self.assertIsNone(normalize_fundamentals(raw)['normalized_financials']['actuals'][0]['period_type'])

    def test_three_future_quarters_keep_definitions_and_unknown_publication_time(self):
        raw = dict(financials(), consensus=consensus([1, 2, 3]))
        normalized = normalize_fundamentals(raw)['consensus_normalized']
        self.assertEqual(normalized['opt_periods'], ['qf', 'saf', 'af'])
        self.assertEqual(normalized['current_index'], 3)
        metric = normalized['forecasts'][0]['metrics'][0]
        self.assertEqual(metric['description'], 'Diluted common stock EPS')
        self.assertEqual(metric['name'], 'EPS(GAAP)')
        self.assertEqual(metric['comp'], 'beat_est')
        self.assertEqual(metric['raw_path'], '/consensus/list/0/details/0')
        self.assertIsNone(normalized['provider_updated_at'])
        self.assertTrue(normalized['source_response_verified'])
        self.assertFalse(normalize_fundamentals(raw)['consensus_live_verified'])
        self.assertEqual(normalized['observed_at'], NOW)
        self.assertFalse(normalized['annual_forecast_verified'])
        self.assertFalse(normalized['coverage']['annual_eps_available'])
        self.assertEqual(normalized['coverage']['future_eps_quarters'], 3)

    def test_complete_distinct_quarters_derive_only_explicit_labelled_annual_sum(self):
        raw = dict(financials(), consensus=consensus([4, 2, 1, 3]))
        normalized = normalize_fundamentals(raw)['consensus_normalized']
        self.assertTrue(normalized['annual_forecast_verified'])
        year = normalized['annual_series'][0]
        self.assertEqual((year['fiscal_year'], year['value_kind'], year['value']), (2027, 'estimate', '6.0'))
        self.assertEqual(year['method'], 'sum_four_fiscal_quarters')
        self.assertIn('quarterly diluted share weights', year['limitation'])
        self.assertIsNone(year['price_unit'])
        self.assertIn('not EPS unit', normalized['verification_scope'])
        raw['consensus']['list'].append(copy.deepcopy(raw['consensus']['list'][0]))
        self.assertFalse(normalize_fundamentals(raw)['consensus_normalized']['annual_forecast_verified'])
        raw['consensus'] = consensus([1, 2, 3, 4], released=[1, 2])
        hybrid = normalize_fundamentals(raw)['consensus_normalized']
        self.assertTrue(hybrid['coverage']['annual_eps_available'])
        self.assertFalse(hybrid['annual_forecast_verified'])
        self.assertEqual(hybrid['annual_series'][0]['value_kind'], 'hybrid_reported_estimate')
        raw['consensus']['list'][0]['fiscal_year'] = 2026
        raw['consensus']['list'][0]['period_text'] = 'Q1 2026'
        self.assertFalse(normalize_fundamentals(raw)['consensus_normalized']['coverage']['annual_eps_available'])

    def test_native_annual_forecast_is_usable_without_invented_timestamp(self):
        raw = dict(financials(), consensus=consensus([1]))
        raw['consensus']['current_period'] = 'af'
        raw['consensus']['list'][0].update(fiscal_period='FY', period_text='FY 2027')
        normalized = normalize_fundamentals(raw)['consensus_normalized']
        self.assertTrue(normalized['annual_forecast_verified'])
        self.assertEqual(normalized['annual_series'][0]['method'], 'native_annual')
        self.assertIsNone(normalized['provider_updated_at'])
        raw['consensus']['list'].append(copy.deepcopy(raw['consensus']['list'][0]))
        self.assertFalse(normalize_fundamentals(raw)['consensus_normalized']['annual_forecast_verified'])

    def test_unknown_release_flag_or_nonnumeric_estimate_does_not_certify_forecast(self):
        for bad in (None, 'yes', 1):
            raw = dict(financials(), consensus=consensus([1, 2, 3, 4]))
            raw['consensus']['list'][0]['details'][0]['is_released'] = bad
            self.assertFalse(normalize_fundamentals(raw)['consensus_normalized']['annual_forecast_verified'])
        raw = dict(financials(), consensus=consensus([1, 2, 3, 4]))
        raw['consensus']['list'][0]['details'][0]['estimate'] = 'NaN'
        self.assertFalse(normalize_fundamentals(raw)['consensus_normalized']['annual_forecast_verified'])

    def test_official_primary_refs_hash_and_exact_text_are_required_for_unit(self):
        raw = financials()
        excerpt = 'TSMC diluted earnings US$4.60 per ADR unit.'
        raw['primary_financials'] = {'symbol': 'TSM', 'facts': [], 'instrument': {
            'quote_currency': 'USD', 'quote_price_unit': 'per_adr', 'is_adr': True,
            'stock_eps_valuation_applicable': True, 'source_verified': True,
            'evidence_refs': ['issuer'], 'source_excerpt': excerpt}}
        with Store(':memory:') as store:
            self.assertIsNone(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'])
            content = 'Official financial statement. ' + excerpt
            source = {'content': content, 'source_url': 'https://pr.tsmc.com/english/news/3346',
                'sha256': hashlib.sha256(content.encode()).hexdigest()}
            store.put('external_evidence', 'issuer', source)
            result = normalize_fundamentals(raw, store=store)
            self.assertEqual(result['instrument_contract']['quote_price_unit'], 'per_adr')
            self.assertTrue(result['instrument_contract']['stock_eps_valuation_applicable'])
            self.assertEqual(result['normalized_financials']['actuals'][0]['value'], '10.5478')
            self.assertIsNone(result['normalized_financials']['actuals'][0]['price_unit'])
            self.assertIsNone(normalize_fundamentals(raw)['instrument_contract']['quote_price_unit'])
            # ADR ratio and USD/ADR EPS may be in separate issuer documents.
            ratio = '1 ADR equals 5 ordinary shares.'
            ratio_source = {'content': ratio, 'source_url': 'https://investor.tsmc.com/english/reports/FS.pdf',
                'sha256': hashlib.sha256(ratio.encode()).hexdigest()}
            store.put('external_evidence', 'ratio', ratio_source)
            instrument = raw['primary_financials']['instrument']
            instrument.update(evidence_refs=['ratio', 'issuer'], source_excerpt=ratio, unit_source_excerpt=excerpt)
            self.assertEqual(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'], 'per_adr')
            store.put('external_evidence', 'issuer', dict(source, content='changed'))
            self.assertIsNone(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'])
            store.put('external_evidence', 'issuer', dict(source, source_url='https://pr.tsmc.com.evil.test/'))
            self.assertIsNone(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'])

    def test_issuer_linked_google_cdn_exact_path_and_symbol_binding(self):
        raw = financials()
        raw.update(symbol='GOOGL', company_profile={'company_name': 'Alphabet'},
            identity=[{'symbol': 'GOOGL.US', 'currency': 'USD'}])
        content = 'Alphabet Class A common stock; earnings per share.'
        raw['primary_financials'] = {'symbol': 'GOOGL', 'facts': [], 'instrument': {
            'quote_currency': 'USD', 'quote_price_unit': 'per_share', 'is_adr': False,
            'stock_eps_valuation_applicable': True, 'evidence_refs': ['issuer'], 'source_excerpt': content}}
        with Store(':memory:') as store:
            source = {'content': content, 'source_url': 'https://s206.q4cdn.com/479360582/files/doc_financials/2026/q2/report.pdf',
                'sha256': hashlib.sha256(content.encode()).hexdigest()}
            store.put('external_evidence', 'issuer', source)
            self.assertEqual(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'], 'per_share')
            store.put('external_evidence', 'issuer', dict(source, source_url='https://s206.q4cdn.com/other/report.pdf'))
            self.assertIsNone(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'])
            raw['primary_financials']['symbol'] = 'TSM'
            store.put('external_evidence', 'issuer', source)
            self.assertIsNone(normalize_fundamentals(raw, store=store)['instrument_contract']['quote_price_unit'])

    def test_daily_reference_keeps_historical_price_and_gate_under_network_block(self):
        calls = []
        class Client:
            def query(self, group, method, *args, **kwargs):
                if method == 'static_info':
                    return financials()['identity']
                return {}
        def fetch(symbols, **kwargs):
            calls.append(symbols)
            return [{'symbol': 'TSM', 'fetched_at': NOW, 'selected': {'symbol': 'TSM', 'price': '400',
                'source': 'longbridge-openapi', 'currency': 'USD', 'session': 'regular',
                'quote_time': '2026-10-23T20:00:00+00:00', 'status': 'historical'}}]
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            result = MarketData(client=Client(), snapshot_fetcher=fetch).fundamentals('TSM', NOW)
        quote = result['reference_quote']
        self.assertEqual(calls, [['TSM']])
        self.assertEqual(quote['selected']['status'], 'historical')
        self.assertEqual(quote['selected']['price'], '400')
        self.assertEqual(quote['selected']['quote_time'], '2026-10-23T20:00:00+00:00')
        self.assertFalse(quote['business_gate']['eligible'])

    def test_native_client_quote_reuses_adapter_closed_selection_and_historical_reference(self):
        def bar(price, at):
            return SimpleNamespace(last_done=price, timestamp=datetime.fromisoformat(at), prev_close='399',
                volume=1, high=price, low=price, turnover=price)
        regular = bar('400', '2026-10-23T20:00:00+00:00')
        regular.symbol = 'TSM.US'
        regular.trade_status = 'TradeStatus.Normal'
        regular.pre_market_quote = None
        regular.post_market_quote = bar('401', '2026-10-24T00:00:00+00:00')
        regular.overnight_quote = None
        queried = []
        class Client:
            def query(self, group, method, *args, **kwargs):
                queried.append((group, method, args))
                if method == 'quote':
                    return [regular]
                if method == 'static_info':
                    return financials()['identity']
                if method == 'quote_package_details':
                    return [{'key': 'US_QBBO_OpenAPI', 'start_at': '2026-10-01T00:00:00+00:00',
                        'end_at': '2026-11-01T00:00:00+00:00'}]
                return {}
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            result = MarketData(client=Client()).fundamentals('TSM', NOW)
        reference = result['reference_quote']
        self.assertNotIn('error', reference)
        self.assertEqual(reference['selected']['status'], 'closed')
        self.assertIsNone(reference['selected']['price'])
        self.assertFalse(reference['business_gate']['eligible'])
        comparison = reference['comparison_quote']
        self.assertEqual(comparison['price'], 401.0)
        self.assertEqual(comparison['session'], 'after_hours')
        self.assertEqual(comparison['status'], 'historical')
        self.assertEqual(comparison['quote_time'], '2026-10-23T20:00:00-04:00')
        self.assertTrue(reference['comparison_only'])
        self.assertEqual([args for group, method, args in queried if method == 'quote'], [(['TSM.US'],)])

    def test_partial_native_records_preserve_unknowns_instead_of_fabricating_zero(self):
        raw = dict(financials(), financial_report={'list': {'IS': {'indicators': None}}},
            consensus={'currency': None, 'current_period': 'qf', 'list': None})
        result = normalize_fundamentals(raw)
        self.assertEqual(result['normalized_financials']['actuals'], [])
        self.assertFalse(result['consensus_live_verified'])
        self.assertIsNone(result['consensus_normalized']['currency'])
        self.assertFalse(result['consensus_normalized']['annual_forecast_verified'])

    def test_etf_gap_clears_only_for_aligned_verified_raw_issuer_components(self):
        raw = financials()
        raw.update(symbol='SOXX', company_profile={'name': 'iShares Semiconductor ETF'},
            identity=[{'symbol': 'SOXX.US', 'currency': 'USD', 'name_en': 'iShares Semiconductor ETF'}])
        content = 'iShares Semiconductor ETF; NAV per Share 500; P/E Ratio 30; holdings 99%.'
        ref = {'evidence_refs': ['issuer'], 'source_excerpt': content}
        raw['primary_financials'] = {'symbol': 'SOXX', 'facts': [], 'instrument': dict(ref,
            is_adr=False, quote_currency='USD', quote_price_unit='per_share', stock_eps_valuation_applicable=False)}
        raw['etf_valuation'] = {'issuer_verified': True,
            'nav': dict(ref, value='500', currency='USD', price_unit='per_share', as_of='2026-10-23'),
            'pe': dict(ref, value='30', as_of='2026-10-23'),
            'holdings': dict(ref, as_of='2026-10-23', coverage='0.99', rows=[{'symbol': 'NVDA'}],
                concentration={'top10_weight': '0.6'})}
        with Store(':memory:') as store:
            store.put('external_evidence', 'issuer', {'content': content,
                'source_url': 'https://www.ishares.com/us/products/239705/fund',
                'sha256': hashlib.sha256(content.encode()).hexdigest()})
            contract = normalize_fundamentals(raw, store=store)['instrument_contract']
            self.assertIsNone(contract['valuation_gap'])
            self.assertFalse(contract['stock_eps_valuation_applicable'])
            self.assertEqual(contract['etf_valuation_as_of'], '2026-10-23')
            raw['etf_valuation']['pe']['as_of'] = '2026-10-22'
            self.assertIsNotNone(normalize_fundamentals(raw, store=store)['instrument_contract']['valuation_gap'])
            raw['etf_valuation']['pe']['as_of'] = '2026-10-23'
            raw['etf_valuation']['holdings']['evidence_refs'] = ['missing']
            self.assertIsNotNone(normalize_fundamentals(raw, store=store)['instrument_contract']['valuation_gap'])


if __name__ == '__main__':
    unittest.main()
