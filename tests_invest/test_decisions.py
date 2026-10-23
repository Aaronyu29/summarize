import copy
import unittest
from rhino_invest.store import Store
from rhino_invest.decisions import evaluate
from rhino_invest.research import seal_plan_review

NOW = '2026-10-23T15:00:00+00:00'


def active_plan(symbol='SOXX'):
    return {'id': symbol + '-v1', 'revision': 1, 'symbol': symbol, 'status': 'active',
        'created_at': '2026-10-22T15:00:00+00:00', 'evidence_refs': ['sealed-evidence'],
        'thesis': 'Independent forecast with explicit scenario assumptions', 'horizon_months': 24,
        'next_review_at': '2027-01-22T15:00:00+00:00', 'currency': 'USD', 'price_unit': 'per_share',
        'entry': {'low': '90', 'high': '110', 'amount_usd': '3000'},
        'targets': [{'scenario': 'base', 'price': '150', 'horizon_months': 24,
            'method': 'eps_multiple', 'assumptions': {'eps': '5', 'multiple': '30'}, 'evidence_refs': ['sealed-evidence']}],
        'take_profit': [{'price': '150', 'fraction': '0.5'}, {'price': '200', 'fraction': '0.5'}],
        'stop': {'price': '80', 'confirmation': 'daily_close'}, 'invalidation_conditions': ['cashflow_failed'],
        'time_exit_policy': 'Review at horizon; hold requires a new thesis', 'valuation_basis': {'type': 'scenario'},
        'verification': {'provenance': True, 'currency': True, 'unit': True}, 'conditions_parsed': True}


def market(symbol='SOXX', price='100'):
    return {'symbol': symbol, 'next_regular_close': '2026-10-23T20:00:00+00:00',
        'selected': {'symbol': symbol, 'price': price, 'currency': 'USD', 'source': 'longbridge-openapi',
            'quote_time': NOW, 'status': 'current', 'session': 'regular', 'permission_verified': True}}


def portfolio(cash='1500', holdings=None):
    # These direct decision fixtures explicitly describe a reconciled inventory,
    # including a confirmed empty inventory when holdings is omitted.
    return {'account': 'real', 'currency': 'USD', 'cash': cash, 'cash_complete': True,
        'cost_complete': True, 'fees_complete': True, 'holdings_complete': True,
        'positions': holdings or [], 'drawdown_limit': None}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.put('research_evidence', 'sealed-evidence', {'id': 'sealed-evidence'}, immutable=True)

    def evaluate(self, plan, snapshot, state, now=NOW):
        if 'review_evidence_id' not in plan:
            seal_plan_review(self.store, plan, {'accepted': True}, plan.get('evidence_refs'))
        return evaluate(self.store, plan, snapshot, state, now)

    def test_default_each_buy_1000_and_cross_symbol_cash_reservation(self):
        rec = self.evaluate(active_plan(), market(), portfolio(), NOW)
        self.assertEqual((rec['action'], rec['amount_usd'], rec['quantity']), ('buy', '1000', '9'))
        self.assertEqual(rec['risk_context']['drawdown_status'], 'unconfigured')
        self.assertFalse(rec['risk_context']['risk_check_passed'])
        other = self.evaluate(active_plan('NVDA'), market('NVDA'), portfolio(), NOW)
        self.assertEqual(other['status'], 'cash_blocked')
        same = self.evaluate(active_plan(), market(), portfolio(), NOW)
        self.assertEqual(same['id'], rec['id'])
        self.assertEqual(len(self.store.items('reservation')), 1)

    def test_2000_requires_explicit_user_selection_not_plan_suggestion(self):
        plan = active_plan()
        self.store.put('plan_control', plan['id'], {'amount_usd': '2000', 'confirmed': True})
        rec = self.evaluate(plan, market(), portfolio('2500'), NOW)
        self.assertEqual(rec['amount_usd'], '2000')

    def test_intraday_cross_does_not_trigger_daily_stop(self):
        held = portfolio(holdings=[{'symbol': 'SOXX', 'quantity': '10'}])
        snap = market(price='70')
        rec = self.evaluate(active_plan(), snap, held, NOW)
        self.assertEqual(rec['action'], 'hold')
        snap['regular_close'] = {'price': '79', 'quote_time': '2026-10-22T20:00:00+00:00',
            'symbol': 'SOXX', 'session_date': '2026-10-22', 'confirmed': True, 'currency': 'USD', 'source': 'longbridge-openapi'}
        snap['latest_completed_regular_session'] = {'date': '2026-10-22', 'close': '2026-10-22T20:00:00+00:00'}
        rec = self.evaluate(active_plan(), snap, held, NOW)
        self.assertEqual((rec['action'], rec['quantity']), ('exit', '10'))

    def test_stale_fx_missing_provenance_and_due_review_never_buy(self):
        for mutation in ('stale', 'fx', 'provenance', 'review'):
            plan, snap = active_plan(), market()
            if mutation == 'stale': snap['selected']['status'] = 'stale'
            if mutation == 'fx': snap['selected']['currency'] = 'TWD'
            if mutation == 'provenance': plan['evidence_refs'] = ['not-stored']
            if mutation == 'review': plan['next_review_at'] = '2026-10-23T14:00:00+00:00'
            result = self.evaluate(plan, snap, portfolio(), NOW)
            self.assertNotIn(result['action'], ['buy', 'add'])
        self.assertEqual(self.store.items('reservation'), [])

    def test_take_profit_only_for_existing_holding(self):
        rec = self.evaluate(active_plan(), market(price='151'),
            portfolio(holdings=[{'symbol': 'SOXX', 'quantity': '10'}]), NOW)
        self.assertEqual((rec['action'], rec['quantity']), ('reduce', '5'))
        rec = self.evaluate(active_plan(), market(price='151'), portfolio(), NOW)
        self.assertEqual(rec['action'], 'watch')

    def test_minute_polls_one_intent_and_explicit_fill_then_rearm(self):
        plan = active_plan()
        first = self.evaluate(plan, market(), portfolio('5000'))
        snap = market(price='101')
        snap['selected']['quote_time'] = '2026-10-23T15:01:00+00:00'
        again = self.evaluate(plan, snap, portfolio('5000'), '2026-10-23T15:01:00+00:00')
        self.assertEqual(first['id'], again['id'])
        self.store.append_event('ledger', 'explicit-fill', {'id': 'explicit-fill', 'account': 'real',
            'symbol': 'SOXX', 'type': 'buy', 'recommendation_id': first['id'], 'timestamp': NOW,
            'currency': 'USD', 'quantity': '9', 'price': '100', 'fee': '1'})
        held = portfolio('4000', [{'symbol': 'SOXX', 'quantity': '9'}])
        after = self.evaluate(plan, snap, held, '2026-10-23T15:01:00+00:00')
        self.assertEqual(after['action'], 'hold')
        self.store.put('plan_control', plan['id'], {'tranche_id': 'user-approved-second-buy', 'confirmed': True})
        rearmed = self.evaluate(plan, snap, held, '2026-10-23T15:01:00+00:00')
        self.assertEqual(rearmed['action'], 'add')
        self.assertNotEqual(rearmed['id'], first['id'])

    def test_unrelated_holding_change_does_not_mark_intent_filled(self):
        plan = active_plan()
        first = self.evaluate(plan, market(), portfolio('5000'))
        unrelated = portfolio('5000', [{'symbol': 'SOXX', 'quantity': '2'}])
        again = self.evaluate(plan, market(), unrelated)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(self.store.get('advice_intent', first['id'])['status'], 'pending')

    def test_tp_cumulative_baseline_and_paper_real_isolation(self):
        plan = active_plan()
        first = self.evaluate(plan, market(price='151'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10'}]))
        repeated = self.evaluate(plan, market(price='152'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10'}]))
        self.assertEqual(first['id'], repeated['id'])
        self.store.append_event('ledger', 'tp-fill', {'id': 'tp-fill', 'account': 'real', 'symbol': 'SOXX',
            'type': 'sell', 'recommendation_id': first['id'], 'timestamp': NOW,
            'currency': 'USD', 'quantity': '5', 'price': '151', 'fee': '1'})
        after = self.evaluate(plan, market(price='152'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '5'}]))
        self.assertEqual(after['action'], 'hold')
        higher = self.evaluate(plan, market(price='201'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '5'}]))
        self.assertEqual((higher['action'], higher['quantity']), ('exit', '5'))
        paper = dict(portfolio('5000'), account='paper')
        paper_rec = self.evaluate(plan, market(), paper)
        self.assertEqual(paper_rec['action'], 'buy')
        self.assertNotEqual(paper_rec['id'], first['id'])

    def test_candidate_stop_tamper_and_old_daily_bar_do_not_exit(self):
        plan = active_plan()
        seal_plan_review(self.store, plan, {'accepted': True})
        plan['status'] = 'candidate'
        snap = market(price='70')
        snap['regular_close'] = {'symbol': 'SOXX', 'session_date': '2026-10-21',
            'price': '79', 'quote_time': '2026-10-21T20:00:00+00:00', 'confirmed': True,
            'currency': 'USD', 'source': 'longbridge-openapi'}
        held = portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10'}])
        self.assertEqual(self.evaluate(plan, snap, held)['action'], 'hold')
        plan['status'] = 'active'
        self.assertEqual(self.evaluate(plan, snap, held)['action'], 'hold')
        plan['stop']['price'] = '99'
        self.assertIn('verified stored evidence missing', self.evaluate(plan, market(), held)['reasons'])

    def test_cash_reservation_retained_outside_zone_and_control_change(self):
        plan = active_plan()
        self.store.put('plan_control', plan['id'], {'amount_usd': '2000', 'confirmed': True})
        first = self.evaluate(plan, market(), portfolio('2500'))
        self.store.put('plan_control', plan['id'], {'amount_usd': '1000', 'confirmed': True})
        self.evaluate(plan, market(price='111'), portfolio('2500'))
        self.assertEqual(self.store.items('reservation')[0][1]['amount_usd'], '2000')
        second = self.evaluate(active_plan('NVDA'), market('NVDA'), portfolio('2500'))
        self.assertEqual(second['status'], 'cash_blocked')
        again = self.evaluate(plan, market(), portfolio('2500'))
        self.assertEqual(first['id'], again['id'])

    def test_actual_holding_horizon_and_ledger_outage_gate(self):
        plan = active_plan()
        held = portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10', 'first_fill_at': '2024-10-01T15:00:00+00:00'}])
        rec = self.evaluate(plan, market(), held)
        self.assertEqual(rec['status'], 'needs_review')
        self.assertGreaterEqual(rec['holding_months_elapsed'], 24)
        outage = dict(portfolio('5000'), ledger_input_current=False)
        rec = self.evaluate(active_plan(), market(), outage)
        self.assertEqual((rec['action'], rec['status']), ('watch', 'data_blocked'))

    def test_pending_sell_does_not_oversell_after_unlinked_position_change(self):
        plan = active_plan()
        first = self.evaluate(plan, market(price='151'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10'}]))
        changed = self.evaluate(plan, market(price='152'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '7'}]))
        self.assertEqual(changed['action'], 'hold')
        self.assertNotEqual(changed['status'], 'actionable')
        self.assertEqual(self.store.get('advice_intent', first['id'])['status'], 'cancelled')

    def test_post_plan_confirmed_split_blocks_old_price_stop_until_review(self):
        plan = active_plan()
        plan['stop']['confirmation'] = 'intraday'
        self.store.append_event('ledger', 'split-after-plan', {'id': 'split-after-plan', 'account': 'real',
            'type': 'split', 'symbol': 'SOXX', 'ratio': '2', 'currency': 'USD',
            'timestamp': '2026-10-23T14:00:00+00:00'})
        rec = self.evaluate(plan, market(price='70'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '20'}]))
        self.assertEqual((rec['action'], rec['status']), ('hold', 'needs_review'))
        self.assertIn('post_plan_split_requires_price_unit_review', rec['reasons'])
        self.assertNotIn('confirmed_price_stop', rec['reasons'])
        self.assertEqual(self.store.items('advice_intent'), [])
        # A confirmed void removes the effective corporate action; raw audit history remains.
        self.store.append_event('ledger', 'void-split', {'id': 'void-split', 'account': 'real',
            'type': 'correction', 'corrects_id': 'split-after-plan', 'replacement': None,
            'currency': 'USD', 'timestamp': '2026-10-23T14:30:00+00:00'})
        rec = self.evaluate(plan, market(price='70'), portfolio('5000', [{'symbol': 'SOXX', 'quantity': '10'}]))
        self.assertEqual((rec['action'], rec['status']), ('exit', 'actionable'))

    def test_future_or_other_account_split_cannot_rewrite_current_real_basis(self):
        plan = active_plan()
        for event_id, account, timestamp in [('future', 'real', '2026-10-23T16:00:00+00:00'),
                ('paper-only', 'paper', '2026-10-23T14:00:00+00:00')]:
            self.store.append_event('ledger', event_id, {'id': event_id, 'account': account, 'type': 'split',
                'symbol': 'SOXX', 'ratio': '2', 'currency': 'USD', 'timestamp': timestamp})
        rec = self.evaluate(plan, market(), portfolio('5000'))
        self.assertEqual(rec['action'], 'buy')


if __name__ == '__main__':
    unittest.main()
