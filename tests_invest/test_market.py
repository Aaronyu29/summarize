import unittest
from unittest.mock import patch
from rhino_invest.market import MarketData, quote_gate

NOW = '2026-10-23T15:00:00+00:00'


class MarketTests(unittest.TestCase):
    def test_etf_contract_preserves_raw_fund_accounts_without_claiming_company_eps(self):
        report = {'list': {'IS': {'indicators': [{'accounts': [{'field': 'EPS', 'name': 'Earning Per Share(USD)'}]}]}}}
        class Client:
            def query(self, group, method, *args, **kwargs):
                if method == 'static_info':
                    return [{'symbol': 'SOXX.US', 'name_en': 'iShares Semiconductor ETF', 'currency': 'USD'}]
                if method == 'financial_report':
                    return report
                if method == 'consensus':
                    return {'currency': '', 'current_period': 'qf', 'list': []}
                return {}
        data = MarketData(client=Client()).fundamentals('SOXX', NOW)
        self.assertEqual(data['financial_report'], report)
        self.assertEqual(data['instrument_contract']['asset_type'], 'etf')
        self.assertFalse(data['instrument_contract']['stock_eps_valuation_applicable'])
        self.assertIsNone(data['instrument_contract']['quote_price_unit'])
        self.assertFalse(data['consensus_live_verified'])
        self.assertTrue(data['consensus_response_received'])
        self.assertEqual(data['consensus_normalized']['coverage']['future_eps_quarters'], 0)

    def test_official_only_and_time_session_gate(self):
        calls = []
        def fetch(symbols, **kwargs):
            calls.append((symbols, kwargs))
            return [{'symbol': 'TSM', 'selected': {'symbol': 'TSM', 'price': '350',
                'currency': 'USD', 'quote_time': NOW, 'source': 'longbridge-openapi',
                'status': 'current', 'session': 'regular', 'permission_verified': True}}]
        result = MarketData(snapshot_fetcher=fetch).snapshots(['tsm', 'TSM', 'SOXX'], NOW)
        self.assertEqual(calls[0][0], ['TSM', 'SOXX'])
        self.assertEqual(calls[0][1]['source'], 'longbridge-api')
        self.assertTrue(result[0]['business_gate']['eligible'])
        self.assertFalse(result[1]['business_gate']['eligible'])
        quote = result[0]['selected']
        for field, value in [('source', 'public-fallback'), ('session', 'after_hours'),
                ('quote_time', '2026-10-23T14:00:00+00:00'), ('price', None)]:
            self.assertFalse(quote_gate(dict(quote, **{field: value}), NOW)['eligible'])

    def test_native_fundamentals_and_partial_failures(self):
        class Client:
            def query(self, group, method, symbol, **kwargs):
                self.symbol = symbol
                if method == 'valuations':
                    raise RuntimeError('secret response suppressed')
                return {'income_statement': {'period': '2025', 'currency': 'TWD'}}
        client = Client()
        data = MarketData(client=client).fundamentals('TSM', NOW)
        self.assertEqual(client.symbol, ['TSM.US'])
        self.assertIn('income_statement', data['financial_report'])
        self.assertIsNone(data['valuations'])
        self.assertFalse(data['adr_conversion_verified'])
        self.assertEqual(data['gaps'], [{'field': 'valuations', 'error': 'RuntimeError'}])

    def test_permission_depth_receipt_and_exact_completed_daily_bar(self):
        class Client:
            def query(self, group, method, *args, **kwargs):
                if method == 'quote_package_details':
                    return [{'key': 'US_QBBO_OpenAPI', 'start_at': '2026-10-01T00:00:00+00:00',
                        'end_at': '2026-11-01T00:00:00+00:00'}]
                if method == 'depth':
                    return {'bids': [{'price': '100', 'volume': 10}], 'asks': [{'price': '101', 'volume': 15}]}
                if method == 'candlesticks':
                    return [{'timestamp': '2026-10-22T13:30:00+00:00', 'close': '99'}]
                raise ValueError(method)
        def fetch(symbols, **kwargs):
            return [{'symbol': 'TSM', 'selected': {'symbol': 'TSM', 'price': '100', 'currency': 'USD',
                'quote_time': NOW, 'source': 'longbridge-openapi', 'status': 'current', 'session': 'regular'}}]
        market = MarketData(client=Client(), snapshot_fetcher=fetch)
        with patch('rhino_invest.market.utc_now', return_value=NOW):
            result = market.snapshots(['TSM'], NOW)[0]
        self.assertTrue(result['business_gate']['eligible'])
        self.assertTrue(result['selected']['permission_verified'])
        self.assertEqual(result['selected']['depth_timestamp_basis'], 'client_receipt')
        self.assertIsNone(result['selected']['depth_size_unit'])
        self.assertIsNone(result['selected']['bid_size'])
        self.assertEqual(result['selected']['raw_bid_volume'], '10')
        self.assertFalse(result['selected']['liquidity_verified'])
        self.assertEqual(result['regular_close']['session_date'], '2026-10-22')
        self.assertEqual(result['regular_close']['quote_time'], '2026-10-22T20:00:00+00:00')
        self.assertFalse(market.permissions('2026-11-02T00:00:00+00:00')['verified'])

    def test_delayed_and_unknown_permission_block_even_fresh_quotes(self):
        quote = {'symbol': 'SOXX', 'price': '100', 'currency': 'USD', 'quote_time': NOW,
                 'source': 'longbridge-openapi', 'status': 'current', 'session': 'regular'}
        self.assertIn('quote_permission_unverified', quote_gate(quote, NOW)['reasons'])
        self.assertIn('delayed_quote', quote_gate(dict(quote, permission_verified=True, delayed=True), NOW)['reasons'])
        self.assertFalse(quote_gate(dict(quote, permission_verified=True,
            quote_time='2026-10-23T14:59:29+00:00'), NOW)['eligible'])


if __name__ == '__main__':
    unittest.main()
