"""Offline completion audit for real holding/risk/renewal lifecycles."""
import unittest

from rhino_invest.decisions import (evaluate, record_thesis_risk, valid_thesis_events,
                                    take_profit_position_basis)
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio
from rhino_invest.research import seal_plan_review
from rhino_invest.store import Store
from tests_invest.test_decisions import active_plan, market, NOW


class CompletionReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.put('research_evidence', 'sealed-evidence', {'id': 'sealed-evidence'}, immutable=True)
        self.store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
        self.plan = active_plan()
        self.seal()
        self.fill('cash', 'opening_cash', amount='20000')

    def seal(self):
        seal_plan_review(self.store, self.plan, {'accepted': True}, self.plan['evidence_refs'])

    def fill(self, identity, kind, **fields):
        entry = {'id': identity, 'account': 'real', 'type': kind, 'currency': 'USD',
                 'timestamp': fields.pop('timestamp', NOW), **fields}
        if kind in {'buy', 'sell', 'opening_position'}:
            entry.setdefault('symbol', 'SOXX')
            entry.setdefault('fee', '0')
        stage_entry(self.store, entry)
        return confirm_entry(self.store, identity)

    def state(self):
        return portfolio(self.store, 'real', now=NOW)

    def risk(self):
        self.store.put('review_event_evidence', 'raw-report', {'id': 'raw-report', 'cashflow': 'failed'}, immutable=True)
        assessment = {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-report'],
                      'rationale': 'The persisted financial report matches the existing condition.'}
        review = {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-report'], 'issues': []}
        return record_thesis_risk(self.store, self.plan, assessment, review, now=NOW)

    def test_independent_invalidation_survives_plan_status_and_is_once_only_exit_intent(self):
        self.fill('opening', 'opening_position', quantity='10', price='100', first_fill_at='2026-01-01T00:00:00Z')
        risk = self.risk()
        self.assertEqual(self.risk(), risk)
        self.plan['status'] = 'invalidated'
        first = evaluate(self.store, self.plan, market(price='100'), self.state(), NOW)
        again = evaluate(self.store, self.plan, market(price='101'), self.state(), NOW)
        self.assertEqual((first['action'], first['status'], first['quantity']), ('exit', 'actionable', '10'))
        self.assertEqual(first['risk_evidence_ids'], [risk['id']])
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len(self.store.items('risk_evidence')), 1)
        self.assertFalse(any(event['type'] == 'sell' for event in self.store.events('ledger')))

    def test_bare_snapshot_risk_and_rejected_independent_review_never_authorize_exit(self):
        self.fill('opening', 'opening_position', quantity='10', price='100')
        snap = market(price='120')
        snap['thesis_events'] = [{'verified': True, 'condition': 'cashflow_failed', 'evidence_refs': ['fake']}]
        self.store.put('risk_evidence', 'fake', {'verified': True, 'condition': 'cashflow_failed'})
        result = evaluate(self.store, self.plan, snap, self.state(), NOW)
        self.assertEqual(result['action'], 'hold')
        with self.assertRaises(ValueError):
            record_thesis_risk(self.store, self.plan,
                {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['fake'], 'rationale': 'claim'},
                {'accepted': False, 'condition': 'cashflow_failed', 'evidence_refs': ['fake']}, now=NOW)

    def test_reported_positive_holding_warns_without_inventing_quantity_or_other_symbols(self):
        self.risk()
        self.plan['status'] = 'invalidated'
        state = self.state()
        state['reported_holdings'] = [{'account': 'real', 'symbol': 'SOXX', 'reported_market_value': '750'}]
        warning = evaluate(self.store, self.plan, market(), state, NOW)
        self.assertEqual((warning['action'], warning['status'], warning['quantity']), ('watch', 'warning', None))
        self.assertIn('reported_holding_quantity_unknown_requires_risk_check', warning['reasons'])
        state['reported_holdings'][0]['symbol'] = 'GOOGL'
        unrelated = evaluate(self.store, self.plan, market(), state, NOW)
        self.assertEqual(unrelated['status'], 'invalidated')
        self.assertIsNone(unrelated['quantity'])

    def test_confirmed_full_exit_beats_an_older_positive_user_statement(self):
        self.fill('opening', 'opening_position', quantity='10', price='100')
        self.fill('closed', 'sell', quantity='10', price='100')
        self.risk()
        self.plan['status'] = 'invalidated'
        state = self.state()
        state['reported_holdings'] = [{'account': 'real', 'symbol': 'SOXX', 'reported_market_value': '750'}]
        result = evaluate(self.store, self.plan, market(price='70'), state, NOW)
        self.assertEqual((result['action'], result['status'], result['quantity']), ('watch', 'invalidated', None))
        self.assertNotIn('reported_holding_quantity_unknown_requires_risk_check', result['reasons'])

    def test_risk_with_stale_quote_warns_and_mutated_raw_source_invalidates_risk(self):
        self.fill('opening', 'opening_position', quantity='10', price='100')
        self.store.put('external_evidence', 'changeable-source', {'cashflow': 'failed'})
        assessment = {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['changeable-source'], 'rationale': 'raw fact'}
        review = {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['changeable-source'], 'issues': []}
        record_thesis_risk(self.store, self.plan, assessment, review, now=NOW)
        snap = market(price='120'); snap['selected']['status'] = 'stale'
        warning = evaluate(self.store, self.plan, snap, self.state(), NOW)
        self.assertEqual((warning['action'], warning['status']), ('exit', 'warning'))
        self.assertTrue(warning['risk_evidence_ids'])
        self.store.put('external_evidence', 'changeable-source', {'cashflow': 'unrelated replacement'})
        self.assertEqual(valid_thesis_events(self.store, self.plan, NOW), [])

    def test_add_after_partial_tp_requires_review_then_exact_reviewed_basis_can_exit_all(self):
        self.fill('opening', 'opening_position', quantity='10', price='100', first_fill_at='2026-01-01T00:00:00Z')
        first = evaluate(self.store, self.plan, market(price='151'), self.state(), NOW)
        self.assertEqual(first['quantity'], '5')
        self.fill('partial-sale', 'sell', quantity='5', price='151', recommendation_id=first['id'])
        self.fill('additional-buy', 'buy', quantity='10', price='100')
        blocked = evaluate(self.store, self.plan, market(price='201'), self.state(), NOW)
        self.assertEqual((blocked['action'], blocked['status']), ('hold', 'needs_review'))
        self.assertIn('position_structure_changed_requires_take_profit_review', blocked['reasons'])
        self.assertIsNone(blocked['quantity'])
        basis = take_profit_position_basis(self.store, self.state(), 'SOXX', NOW)
        self.plan.update(revision=2, created_at=NOW, take_profit_position_basis={'real': basis})
        self.seal()
        reviewed = evaluate(self.store, self.plan, market(price='201'), self.state(), NOW)
        self.assertEqual((reviewed['action'], reviewed['quantity']), ('exit', '15'))
        self.fill('final-sale', 'sell', quantity='15', price='201', recommendation_id=reviewed['id'])
        self.assertEqual(self.state()['positions'][0]['quantity'], '0')

    def test_other_symbol_changes_do_not_reanchor_tp_but_relevant_correction_does(self):
        self.fill('opening', 'opening_position', quantity='10', price='100')
        first = evaluate(self.store, self.plan, market(price='151'), self.state(), NOW)
        self.fill('other', 'buy', symbol='NVDA', quantity='2', price='100')
        same = evaluate(self.store, self.plan, market(price='151'), self.state(), NOW)
        self.assertEqual(first['id'], same['id'])
        self.fill('correct-opening', 'correction', corrects_id='opening', replacement={
            'id': 'opening', 'account': 'real', 'type': 'opening_position', 'symbol': 'SOXX',
            'currency': 'USD', 'timestamp': NOW, 'quantity': '10', 'price': '95', 'fee': '0'})
        blocked = evaluate(self.store, self.plan, market(price='151'), self.state(), NOW)
        self.assertEqual(blocked['status'], 'needs_review')
        self.assertIsNone(blocked['quantity'])

    def test_reviewed_horizon_renews_thesis_without_resetting_actual_holding_days(self):
        self.fill('opening', 'opening_position', quantity='10', price='100', first_fill_at='2024-01-01T00:00:00Z')
        old = evaluate(self.store, self.plan, market(price='120'), self.state(), NOW)
        self.assertEqual(old['status'], 'needs_review')
        self.plan.update(revision=2, created_at=NOW, thesis_valid_from=NOW,
                         horizon_until='2028-10-23T15:00:00+00:00')
        self.seal()
        renewed = evaluate(self.store, self.plan, market(price='120'), self.state(), NOW)
        self.assertEqual((renewed['action'], renewed['status']), ('hold', 'waiting'))
        self.assertEqual(renewed['holding_days'], old['holding_days'])
        self.assertGreater(renewed['holding_days'], 1000)
        self.plan['horizon_until'] = '2029-10-23T15:00:00Z'
        tampered = evaluate(self.store, self.plan, market(price='120'), self.state(), NOW)
        self.assertEqual(tampered['status'], 'needs_review')
        self.assertIn('reviewed_thesis_horizon_invalid', tampered['reasons'])

    def test_user_confirmed_per_buy_amount_does_not_impose_a_cumulative_position_cap(self):
        self.fill('opening', 'opening_position', quantity='50', price='100')
        result = evaluate(self.store, self.plan, market(price='100'), self.state(), NOW)
        self.assertEqual((result['action'], result['amount_usd']), ('add', '1000'))

    def test_previously_verified_inactive_or_due_stop_still_warns_without_sell_order(self):
        self.fill('opening', 'opening_position', quantity='10', price='100')
        self.plan['stop']['confirmation'] = 'intraday'
        self.plan['next_review_at'] = '2026-10-23T14:00:00Z'
        self.seal()
        for status in ('active', 'needs_review', 'invalidated', 'superseded'):
            self.plan['status'] = status
            warning = evaluate(self.store, self.plan, market(price='70'), self.state(), NOW)
            self.assertEqual(warning['status'], 'warning')
            self.assertIn('confirmed_price_stop', warning['reasons'])
            self.assertIsNone(warning['quantity'])
            self.assertEqual(self.store.items('advice_intent'), [])

    def test_reported_quantity_unknown_gets_verified_stop_warning_but_draft_prices_do_not(self):
        self.plan['stop']['confirmation'] = 'intraday'
        self.seal()
        self.plan['status'] = 'needs_review'
        state = self.state()
        state['reported_holdings'] = [{'account': 'real', 'symbol': 'SOXX', 'reported_market_value': '750'}]
        warning = evaluate(self.store, self.plan, market(price='70'), state, NOW)
        self.assertEqual((warning['action'], warning['status'], warning['quantity']), ('watch', 'warning', None))
        self.assertIn('confirmed_price_stop', warning['reasons'])
        self.assertIn('reported_holding_quantity_unknown_requires_risk_check', warning['reasons'])
        self.plan['stop']['price'] = '99'  # New unreviewed draft price breaks its original seal.
        draft = evaluate(self.store, self.plan, market(price='70'), state, NOW)
        self.assertNotEqual(draft['status'], 'warning')
        self.assertNotIn('confirmed_price_stop', draft['reasons'])
        self.assertIsNone(draft['quantity'])

    def test_confirmed_split_prevents_even_a_previously_verified_stop_warning(self):
        self.fill('opening', 'opening_position', quantity='10', price='100', timestamp='2026-10-23T13:00:00Z')
        self.plan['stop']['confirmation'] = 'intraday'
        self.seal()
        self.plan['status'] = 'invalidated'
        self.fill('split', 'split', symbol='SOXX', ratio='2', timestamp='2026-10-23T14:00:00Z')
        warning = evaluate(self.store, self.plan, market(price='70'), self.state(), NOW)
        self.assertEqual(warning['status'], 'needs_review')
        self.assertNotIn('confirmed_price_stop', warning['reasons'])
        self.assertIn('post_plan_split_requires_price_unit_review', warning['reasons'])


if __name__ == '__main__':
    unittest.main()
