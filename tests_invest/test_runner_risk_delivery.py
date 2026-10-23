import copy
import unittest
from unittest.mock import patch

from rhino_invest.common import stable_id
from rhino_invest.config import Config
from rhino_invest.decisions import record_thesis_risk
from rhino_invest.ledger import stage_entry, confirm_entry
from rhino_invest.reporting import record_reported_holding
from rhino_invest.research import seal_plan_review
from rhino_invest.runner import Runner
from rhino_invest.store import Store
from tests_invest.test_acceptance_core import approved_plan, quote, FixtureCalendar
from tests_invest.test_runner_delivery import Feishu, Market


NOW = '2026-10-23T15:00:00+00:00'


class RiskDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.now = NOW
        self.plan = approved_plan('SOXX')
        self.store.put('research_evidence', 'sealed:1', {'id': 'sealed:1'}, immutable=True)
        seal_plan_review(self.store, self.plan, {'accepted': True}, self.plan['evidence_refs'])
        self.store.put('plan', self.plan['id'], self.plan)
        self.store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
        self.fill('cash', 'opening_cash', amount='20000')
        self.feishu, self.market, self.sent = Feishu(), Market(), []
        self.feishu.notify = lambda key, text: self.sent.append((key, text)) or {'status': 'provider_ack'}
        self.market.snapshots = lambda symbols, now=None: [quote(now, symbol=s) for s in symbols]
        self.runner = Runner(self.store, Config(symbols=('SOXX',)), feishu=self.feishu, market=self.market,
                             calendar=FixtureCalendar(), execute=True, clock=lambda: self.now)

    def fill(self, identity, kind, account='real', **fields):
        entry = dict(id=identity, account=account, type=kind, currency='USD', timestamp=NOW, **fields)
        stage_entry(self.store, entry)
        confirm_entry(self.store, identity)

    def risk(self):
        self.store.put('review_event_evidence', 'raw-fact', {'cashflow': 'failed'}, immutable=True)
        assessment = {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-fact'], 'rationale': 'persisted fact'}
        review = {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-fact'], 'issues': []}
        return record_thesis_risk(self.store, self.plan, assessment, review, now=NOW)

    def test_confirmed_stop_with_stale_quote_sends_once_risk_warning_not_executable_instruction(self):
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        def stale(symbols, now=None):
            rows = []
            for symbol in symbols:
                row = quote('2026-10-23T14:00:00Z', symbol=symbol)
                row['regular_close'] = {'symbol': symbol, 'price': '70', 'currency': 'USD',
                    'source': 'longbridge-openapi', 'confirmed': True, 'session_date': '2026-10-22',
                    'quote_time': '2026-10-22T20:00:00Z'}
                rows.append(row)
            return rows
        self.market.snapshots = stale
        result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['recommendations'][0]['status'], 'warning')
        messages = [text for _, text in self.sent if text.startswith('【持仓风险')]
        self.assertEqual(len(messages), 1)
        self.assertIn('$80.00', messages[0])
        self.assertIn('2.00股', messages[0])
        self.assertIn('当前行情不可用于执行', messages[0])
        self.assertIn('不是下单指令', messages[0])
        self.assertTrue(any('持仓风险需人工核查' in text for _, text in self.sent if '每日复盘' in text))
        self.now = '2026-10-23T15:01:00Z'
        self.runner.run_once()
        self.assertEqual(len([text for _, text in self.sent if '持仓风险' in text and '【持仓风险' in text]), 1)

    def test_invalidated_reported_holding_warns_without_sell_quantity_or_unrelated_position(self):
        self.risk()
        self.store.put('plan', self.plan['id'], dict(self.plan, status='invalidated'))
        record_reported_holding(self.store, 'SOXX', '750', reported_at=NOW)
        result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        messages = [text for _, text in self.sent if text.startswith('【持仓风险')]
        self.assertEqual(len(messages), 1)
        self.assertIn('股数待核对', messages[0])
        self.assertIn('$750.00', messages[0])
        self.assertNotIn('0.00股', messages[0])
        self.assertNotIn('卖出', messages[0])
        self.assertIn('请先核对股数', messages[0])
        self.assertNotIn('当前行情不可用于执行', messages[0])
        self.assertFalse(any(e['type'] == 'sell' for e in self.store.events('ledger')))

    def test_old_reported_value_does_not_resurrect_after_confirmed_full_sale(self):
        record_reported_holding(self.store, 'SOXX', '750', reported_at=NOW)
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        self.fill('closed', 'sell', symbol='SOXX', quantity='2', price='100', fee='0')
        self.risk()
        self.store.put('plan', self.plan['id'], dict(self.plan, status='invalidated'))
        result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['portfolio']['reported_holdings'], [])
        self.assertEqual(result['portfolio']['positions'][0]['quantity'], '0')
        self.assertEqual(len(self.store.items('reported_holding')), 1)
        self.assertFalse(any(text.startswith('【持仓风险') for _, text in self.sent))

    def test_same_position_structure_only_reviews_once_and_renewed_horizon_gets_new_fact(self):
        snapshot = {'account': 'real', 'positions': [{'symbol': 'SOXX', 'first_fill_at': NOW}]}
        basis = {'account': 'real', 'symbol': 'SOXX', 'first_fill_at': NOW, 'structure_hash': 'actual-ledger-structure'}
        advice = {'reasons': ['position_structure_changed_requires_take_profit_review'], 'take_profit_position_basis': basis}
        self.runner._queue_advice_review(self.plan, advice, {}, snapshot, NOW)
        key, trigger = self.store.items('review_trigger')[0]
        self.store.put('review_trigger', key, dict(trigger, processed=True))
        self.runner._queue_advice_review(dict(self.plan, revision=9, status='needs_review'), advice, {}, snapshot, NOW)
        self.assertEqual(len(self.store.items('review_trigger')), 1)
        self.assertTrue(self.store.get('review_trigger', key)['processed'])
        horizon_advice = {'reasons': ['investment_horizon_expired_requires_new_thesis']}
        old = dict(self.plan, thesis_valid_from='2024-01-01T00:00:00Z', horizon_until='2026-01-01T00:00:00Z')
        self.runner._queue_advice_review(old, horizon_advice, {}, snapshot, NOW)
        renewed = dict(self.plan, revision=2, thesis_valid_from='2026-01-01T00:00:00Z', horizon_until='2028-01-01T00:00:00Z')
        self.runner._queue_advice_review(renewed, horizon_advice, {}, snapshot, NOW)
        self.assertEqual(sum(value['kind'] == 'holding_horizon' for _, value in self.store.items('review_trigger')), 2)

    def test_superseded_revision_keeps_verified_unconsumed_risk_context(self):
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        self.risk()
        self.store.put('plan_revision', stable_id(self.plan['id'], 1), self.plan, immutable=True)
        current = dict(self.plan, revision=2, status='needs_review')
        self.store.put('plan', current['id'], current)
        result = self.runner.run_once()
        self.assertEqual(result['errors'], [])
        self.assertTrue(any(text.startswith('【持仓风险') for _, text in self.sent))
        self.assertEqual(self.store.get('plan', current['id'])['revision'], 2)

    def test_unapproved_revision_changes_do_not_create_paid_review_feedback_loop(self):
        self.store.put('plan', self.plan['id'], dict(self.plan, status='needs_review'))
        self.runner.model = object()
        def rejected(store, plan, model, **kwargs):
            store.put('plan', plan['id'], dict(plan, revision=plan['revision'] + 1, status='needs_review'))
        with patch('rhino_invest.runner.review_plan', side_effect=rejected) as review:
            for revision in (2, 3, 4):
                self.store.put('plan', self.plan['id'], dict(self.plan, revision=revision, status='needs_review'))
                self.runner.run_once()
            self.assertEqual(review.call_count, 0)
        self.assertEqual(self.store.items('review_trigger'), [])
        self.assertEqual(self.store.items('model_call'), [])

    def test_independently_verified_invalidation_never_requeues_itself_for_paid_research(self):
        risk = self.risk()
        self.store.put('plan', self.plan['id'], dict(self.plan, status='invalidated'))
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        self.runner.model = object()
        with patch('rhino_invest.runner.review_plan') as review:
            for minute in range(3):
                self.now = f'2026-10-23T15:0{minute}:00Z'
                result = self.runner.run_once()
                self.assertEqual(result['errors'], [])
                self.runner._research_cycle(['SOXX'], {})
            review.assert_not_called()
        self.assertEqual(self.store.items('review_trigger'), [])
        self.assertEqual([identity for identity, _ in self.store.items('risk_evidence')], [risk['id']])
        self.assertEqual(self.store.items('model_call'), [])

    def test_legacy_drafts_and_reviewed_risk_are_consumed_without_model_in_both_loops(self):
        self.runner.model = object()
        with patch('rhino_invest.runner.review_plan') as review:
            for mode in ('sync', 'background'):
                key = 'legacy-' + mode
                self.store.put('review_trigger', key, {'id': key, 'kind': 'risk_change', 'processed': False,
                    'symbol': 'SOXX', 'evidence': {'status': 'needs_review', 'reasons': ['plan_not_active', 'missing valuation']}})
                self_key = 'self-risk-' + mode
                self.store.put('review_trigger', self_key, {'id': self_key, 'kind': 'verified_thesis_risk',
                    'processed': False, 'symbol': 'SOXX', 'evidence': {'risk_evidence_ids': ['sealed-risk']}})
                if mode == 'sync':
                    self.runner.run_once()
                else:
                    self.runner._research_cycle(['SOXX'], {})
                self.assertEqual(self.store.get('review_trigger', key)['reason'], 'no_external_fact')
                self.assertEqual(self.store.get('review_trigger', self_key)['reason'], 'already_reviewed')
                self.assertTrue(self.store.get('review_trigger', key)['processed'])
            review.assert_not_called()
            self.assertEqual(self.store.items('model_call'), [])
            for code in ('confirmed_price_stop', 'scheduled_thesis_review_due', 'investment_horizon_expired_requires_new_thesis'):
                trigger = {'kind': 'risk_change', 'processed': False, 'evidence': {'reasons': [code]}}
                self.assertFalse(self.runner._consume_legacy_review('valid-' + code, trigger))

    def test_unresolved_verified_risk_is_durable_deferral_not_model_failure_or_alert_spam(self):
        self.runner.model = object()
        self.runner.config.archive_database = 'offline-fixture'
        with patch('rhino_invest.runner.ingest_archives', return_value={
                'errors': [], 'plans': [], 'deferred_unresolved_risk': [{'code': 'UNRESOLVED_VERIFIED_THESIS_RISK'}]}):
            self.runner._research_cycle(['SOXX'], {})
        summary = self.store.get('research_worker', 'latest')
        self.assertEqual(summary['status'], 'deferred')
        self.assertEqual(summary['error_count'], 0)
        self.assertEqual(summary['ingestion']['deferred_unresolved_risk_count'], 1)
        self.assertEqual(summary['deferred'][0]['reason'], 'UNRESOLVED_VERIFIED_THESIS_RISK')
        self.runner._observe_health({'errors': []}, NOW)
        self.runner._observe_health({'errors': []}, NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('已核实风险尚未解除', self.sent[0][1])

    def test_real_fundamental_fact_and_fixed_review_due_still_queue_once_across_revision(self):
        self.runner.model = object()
        self.store.put('fundamental_cache', 'SOXX', {'period': 'older-session', 'data': {'symbol': 'SOXX', 'revenue': 'old'}})
        due = dict(self.plan, next_review_at='2026-10-23T14:00:00Z', status='needs_review')
        self.store.put('plan', due['id'], due)
        def rejected(store, plan, model, **kwargs):
            store.put('plan', plan['id'], dict(plan, revision=plan['revision'] + 1, status='needs_review'))
        with patch('rhino_invest.runner.review_plan', side_effect=rejected) as review:
            self.runner.run_once()
            first = review.call_count
            self.assertGreaterEqual(first, 2)
            for _ in range(3):
                self.runner.run_once()
            self.assertEqual(review.call_count, first)
        kinds = {item['kind'] for _, item in self.store.items('review_trigger')}
        self.assertIn('fundamental_change', kinds)
        self.assertIn('scheduled_review', kinds)

    def test_paper_verified_exit_can_fill_after_invalidation_and_on_next_quote_only(self):
        self.risk()
        self.store.put('plan', self.plan['id'], dict(self.plan, status='invalidated'))
        # Paper cash is an independent model opening baseline.
        from rhino_invest.paper import paper_account
        paper_account(self.store)
        self.fill('paper-held', 'opening_position', account='paper', symbol='SOXX', quantity='2', price='100', fee='0')
        first = self.runner.run_once()
        self.assertEqual(first['errors'], [])
        self.assertEqual(self.store.items('paper_fill'), [])
        pending = next(rec for rec in first['paper_recommendations'] if rec['action'] == 'exit')
        self.now = '2026-10-23T15:01:00Z'
        second = self.runner.run_once()
        self.assertEqual(second['errors'], [])
        self.assertEqual(self.store.get('paper_fill', pending['id'])['status'], 'filled')
        self.assertIsNone(self.store.get('paper_cancel', pending['id']))


if __name__ == '__main__':
    unittest.main()
