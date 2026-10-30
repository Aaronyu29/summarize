"""Primary-review regressions for real exchange boundaries and actual source archives."""
import unittest
from types import SimpleNamespace

from rhino_invest.market import MarketData, latest_regular_session, next_regular_close, quote_gate


class PrimaryReviewTests(unittest.TestCase):
    def test_unimplemented_stop_policy_cannot_silently_activate(self):
        from rhino_invest.common import validate_plan
        from test_core import plan
        for mode in ('weekly_close', 'manual'):
            candidate = plan()
            candidate['stop']['confirmation'] = mode
            self.assertTrue(any('intraday/daily_close only' in e for e in validate_plan(candidate)))

    def test_fresh_last_trade_is_not_executable_after_verified_exchange_close(self):
        # Friday after Thanksgiving closes at 13:00 New York, not 16:00.
        now = '2026-11-27T18:00:05+00:00'
        selected = {'symbol': 'TSM', 'price': '400', 'source': 'longbridge-openapi',
                    'status': 'current', 'currency': 'USD', 'session': 'regular',
                    'permission_verified': True, 'delayed': False,
                    'quote_time': '2026-11-27T17:59:59+00:00'}
        result = quote_gate({'selected': selected, 'execution_session_open': False}, now)
        self.assertFalse(result['eligible'])
        self.assertIn('verified_regular_session_closed', result['reasons'])
        self.assertEqual(latest_regular_session(now)['date'], '2026-11-27')
        self.assertEqual(next_regular_close(now), '2026-11-30T21:00:00+00:00')

    def test_holiday_does_not_fabricate_a_daily_close(self):
        now = '2026-11-26T22:00:00+00:00'
        completed = latest_regular_session(now)
        self.assertEqual(completed['date'], '2026-11-25')
        self.assertEqual(next_regular_close(now), '2026-11-27T18:00:00+00:00')

    def test_extended_session_snapshot_is_preserved_without_becoming_executable(self):
        observed = []
        def fetch(symbols, **kwargs):
            observed.append(kwargs['max_age'])
            return [{'symbol': 'TSM', 'selected': {'symbol': 'TSM', 'price': '400',
                'source': 'longbridge-openapi', 'status': 'current', 'currency': 'USD',
                'session': 'premarket', 'permission_verified': True,
                'quote_time': '2026-10-23T12:59:00+00:00'}}]
        result = MarketData(snapshot_fetcher=fetch).snapshots(['TSM'], '2026-10-23T13:00:00+00:00')[0]
        self.assertEqual(observed, [120])
        self.assertEqual(result['business_gate']['max_age_seconds'], 120)
        self.assertEqual(result['business_gate']['reasons'], ['regular_session_required'])

    def test_runner_publishes_source_zone_watch_without_an_active_plan(self):
        from rhino_invest.config import Config
        from rhino_invest.runner import Runner
        from rhino_invest.store import Store
        from test_watch import VideoWatchTests, NOW
        class Market:
            def snapshots(self, symbols, **kwargs):
                self.symbols = symbols
                return [VideoWatchTests.quote(None)]
            def fundamentals(self, *args, **kwargs):
                return {}
        market = Market()
        with Store(':memory:') as store:
            VideoWatchTests.level(SimpleNamespace(store=store))
            runner = Runner(store, Config(), market=market, execute=False, clock=lambda: NOW)
            first = runner.run_once()
            second = runner.run_once()
            self.assertEqual(market.symbols, ['SOXX'])
            self.assertEqual(len(first['watch_events']), 1)
            self.assertEqual(second['watch_events'], [])
            reports = [r for _, r in store.items('report') if r['kind'] == '价格观察']
            self.assertEqual(len(reports), 1)
            self.assertIn('不构成买入建议', reports[0]['body'])
            self.assertEqual(first['recommendations'], [])
            self.assertEqual(store.items('paper_fill'), [])


if __name__ == '__main__':
    unittest.main()
