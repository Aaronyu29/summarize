import unittest
from unittest.mock import patch

from rhino_invest.config import Config
from rhino_invest.store import Store
from rhino_invest.runner import Runner
from rhino_invest.research import seal_plan_review
from tests_invest.test_acceptance_core import approved_plan, quote, FixtureCalendar


class Calendar:
    def state(self, now):
        return {'source': 'fixture', 'completed_session': '2026-10-23', 'completed_close': '2026-10-23T20:00:00Z',
                'next_regular_close': '2026-10-26T20:00:00Z', 'weekly_due': True,
                'quarter_end_due': False, 'week': '2026-W43', 'quarter': '2026-Q4'}


class Market:
    def __init__(self):
        self.quote_calls = 0
    def snapshots(self, symbols, now=None):
        self.quote_calls += 1
        return [{'symbol': s, 'selected': {'symbol': s, 'price': '100', 'status': 'current',
                'source': 'longbridge-openapi', 'currency': 'USD', 'session': 'regular', 'quote_time': now}} for s in symbols]
    def fundamentals(self, symbol, now=None):
        return {'symbol': symbol, 'financial_report': {}, 'fetched_at': now}


class Feishu:
    app_token = 'fixture'
    def __init__(self):
        self.calls = []
        self.fail_input = False
    def poll_inputs(self):
        if self.fail_input:
            raise TimeoutError('private')
        return {'errors': []}
    def upsert(self, table, fields):
        self.calls.append((table, fields))
        return {'record': {'record_id': 'a'}}
    def notify(self, key, text):
        return {'status': 'disabled'}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.config, self.market, self.feishu = Config(symbols=('TSM',)), Market(), Feishu()
        self.now = '2026-10-23T20:10:00Z'
        self.runner = Runner(self.store, self.config, market=self.market, feishu=self.feishu,
                             calendar=Calendar(), execute=True, clock=lambda: self.now)
    def tearDown(self):
        self.store.close()

    def test_restart_schedule_dedup_and_no_model_while_unconfigured(self):
        first = self.runner.run_once()
        self.assertEqual(len(first['reports']), 4)  # daily+weekly, real+paper
        writes = len(self.feishu.calls)
        self.now = '2026-10-23T20:11:00Z'
        second = self.runner.run_once()
        self.assertEqual(second['reports'], [])
        self.assertEqual(len(self.feishu.calls), writes)
        self.assertEqual(self.market.quote_calls, 2)
        self.assertEqual(self.store.items('model_call'), [])
        self.assertIsNone(first['portfolio']['cash'])

    def test_quote_failure_keeps_worker_alive_and_records_unknown(self):
        self.market.snapshots = lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError('secret'))
        result = self.runner.run_once()
        self.assertEqual(result['quotes'], [])
        self.assertTrue(any(e['stage'] == 'official_quotes' for e in result['errors']))
        self.assertNotIn('secret', str(result))
        self.assertIsNotNone(self.store.get('worker', 'latest'))

    def test_ledger_poll_failure_is_passed_to_decision_gate(self):
        self.feishu.fail_input = True
        self.store.put('plan', 'p1', {'id': 'p1', 'symbol': 'TSM', 'status': 'active'})
        seen = []
        def decide(store, plan, quote, portfolio, now=None):
            seen.append(portfolio)
            return {'id': 'r' + portfolio['account'], 'symbol': 'TSM', 'account': portfolio['account'], 'action': 'watch', 'status': 'data_blocked', 'reasons': []}
        with patch('rhino_invest.runner.evaluate', side_effect=decide):
            self.runner.run_once()
        real = next(p for p in seen if p['account'] == 'real')
        self.assertFalse(real['ledger_input_current'])

    def test_paper_pending_replayed_before_new_signals(self):
        self.store.put('plan', 'p1', {'id': 'p1', 'symbol': 'TSM', 'revision': 1, 'status': 'active', 'next_review_at': '2027-01-01T00:00:00Z'})
        self.store.put('recommendation', 'previous', {'id': 'previous', 'account': 'paper', 'plan_id': 'p1', 'revision': 1, 'status': 'actionable'})
        self.store.put('advice_intent', 'previous', {'status': 'pending'})
        with patch('rhino_invest.runner.simulate', return_value={'status': 'skipped'}) as simulate, patch('rhino_invest.runner.evaluate', return_value={'id': 'previous', 'action': 'buy', 'status': 'actionable'}):
            self.runner.run_once()
        self.assertEqual(simulate.call_count, 1)
        self.assertEqual(simulate.call_args.args[1]['id'], 'previous')

    def test_paper_replay_rejects_invalidated_or_missing_plan(self):
        self.store.put('recommendation', 'stale', {'id': 'stale', 'account': 'paper', 'plan_id': 'gone', 'revision': 1, 'status': 'actionable'})
        self.store.put('advice_intent', 'stale', {'status': 'pending'})
        with patch('rhino_invest.runner.simulate') as simulate:
            self.runner.run_once()
        self.assertEqual(simulate.call_count, 0)
        self.assertIsNotNone(self.store.get('paper_cancel', 'stale'))

    def _approved_fixture(self):
        self.store.put('research_evidence', 'sealed:1', {'id': 'sealed:1', 'source_type': 'sealed_analysis',
            'analysis_sha256': 'a' * 64, 'transcript_sha256': 'b' * 64, 'visual_sha256': 'c' * 64}, immutable=True)
        plan = approved_plan()
        seal_plan_review(self.store, plan, {'accepted': True, 'issues': []}, plan['evidence_refs'])
        self.store.put('plan', plan['id'], plan)
        self.config.symbols = ('SOXX',)
        self.runner.calendar = FixtureCalendar()
        return plan

    def test_clock_refresh_allows_actual_post_fetch_depth_to_fill_next_tick(self):
        self._approved_fixture()
        self.now = '2026-10-23T15:00:00+00:00'
        owner = self
        class AdvancingMarket(Market):
            def snapshots(self, symbols, now=None):
                minute = now[14:16]
                fetched = f'2026-10-23T15:{minute}:02+00:00'
                owner.now = fetched
                snapshots = [quote(fetched, symbol=s) for s in symbols]
                return snapshots
        self.runner.market = AdvancingMarket()
        first = self.runner.run_once()
        self.assertEqual(first['timestamp'], '2026-10-23T15:00:02+00:00')
        self.assertEqual(self.store.items('paper_fill'), [])
        self.now = '2026-10-23T15:01:00+00:00'
        second = self.runner.run_once()
        fills = self.store.items('paper_fill')
        self.assertEqual(second['timestamp'], '2026-10-23T15:01:02+00:00')
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][1]['timestamp'], second['timestamp'])

    def test_synchronous_research_elapsed_time_ages_quote_before_decision(self):
        self._approved_fixture()
        self.now = '2026-10-23T15:00:00+00:00'
        self.config.archive_database = 'fixture-read-only.sqlite'
        self.runner.market.snapshots = lambda symbols, now=None: [quote(now, symbol=s) for s in symbols]
        def delayed_ingest(*_args, **_kwargs):
            self.now = '2026-10-23T15:02:00+00:00'
            return {'ingested': 0, 'plans': [], 'errors': []}
        with patch('rhino_invest.runner.ingest_archives', side_effect=delayed_ingest):
            result = self.runner.run_once()
        self.assertEqual(result['timestamp'], self.now)
        self.assertFalse(self.store.get('market_latest', 'SOXX')['business_gate']['eligible'])
        paper_recs = [r for _, r in self.store.items('recommendation') if r['account'] == 'paper']
        self.assertEqual(paper_recs[-1]['status'], 'data_blocked')
        self.assertIn('quote_timestamp_not_fresh', paper_recs[-1]['reasons'])

    def test_processed_review_trigger_is_not_rearmed_by_identical_next_tick(self):
        plan = {'id': 'p1', 'symbol': 'TSM', 'revision': 3, 'status': 'needs_review'}
        self.store.put('plan', 'p1', plan)
        recommendation = {'id': 'r1', 'symbol': 'TSM', 'action': 'watch', 'status': 'needs_review',
                          'reasons': ['scheduled_thesis_review_due']}
        with patch('rhino_invest.runner.evaluate', return_value=recommendation):
            self.runner.run_once()
            key, trigger = self.store.items('review_trigger')[0]
            self.store.put('review_trigger', key, dict(trigger, processed=True, processed_at=self.now))
            processed = self.store.get('review_trigger', key)
            self.now = '2026-10-23T20:11:00Z'
            self.runner.run_once()
        self.assertEqual(self.store.get('review_trigger', key), processed)


if __name__ == '__main__':
    unittest.main()
