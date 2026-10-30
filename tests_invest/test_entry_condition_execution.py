"""Entry predicates need independent coverage; a boolean never resolves earnings."""
import copy
from datetime import timedelta
import unittest

from rhino_invest.common import classify_plan_validation, entry_condition_errors, parse_time
from rhino_invest.decisions import evaluate
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio
from rhino_invest.paper import paper_account, simulate
from rhino_invest.research import seal_plan_review
from rhino_invest.store import Store
from tests_invest.test_acceptance_core import approved_plan, quote


NOW = '2026-10-23T15:00:00+00:00'
FUTURE = 'Only enter after the November 4, 2026 issuer earnings release confirms the modeled margin improvement'


class EntryConditionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.put('research_evidence', 'sealed:1', {'id': 'sealed:1'}, immutable=True)
        self.store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
        self.record('cash', 'opening_cash', amount='20000')

    def record(self, identity, kind, **fields):
        stage_entry(self.store, {'id': identity, 'account': 'real', 'type': kind, 'currency': 'USD',
            'timestamp': '2026-10-22T15:00:00Z', **fields})
        confirm_entry(self.store, identity)

    def seal(self, plan, *, independent=True, result=None):
        review = {'accepted': True, 'issues': []}
        if independent and plan.get('entry_condition_review'):
            review['entry_condition_review'] = copy.deepcopy(plan['entry_condition_review'])
        review.update(result or {})
        seal_plan_review(self.store, plan, review, plan['evidence_refs'])
        self.store.put('plan', plan['id'], plan)

    def decide(self, plan, *, account='real', now=NOW, price='100'):
        return evaluate(self.store, plan, quote(now, price, symbol=plan['symbol']),
                        portfolio(self.store, account, now=now), now=now)

    def test_legacy_future_earnings_true_boolean_cannot_buy_or_reserve_in_either_account(self):
        plan = approved_plan('CRCL')
        plan['entry'].update(basis='independent_valuation', conditions=[FUTURE])
        plan['entry'].pop('condition_mode')
        plan['entry'].pop('execution_conditions')
        plan.pop('entry_condition_review')
        plan['conditions_parsed'] = True
        self.seal(plan, independent=False)
        paper_account(self.store)
        for account in ('real', 'paper'):
            result = self.decide(plan, account=account)
            self.assertNotEqual(result['status'], 'actionable')
            self.assertIsNone(result['quantity'])
            self.assertIn('entry_conditions_unverified', result['reasons'])
        self.assertEqual(self.store.items('reservation'), [])
        self.assertEqual(self.store.items('advice_intent'), [])
        self.assertEqual(self.store.items('paper_fill'), [])

    def test_author_price_only_declaration_and_fake_review_object_cannot_replace_independent_result(self):
        plan = approved_plan('CRCL')
        plan['entry']['conditions'] = [FUTURE]
        plan['entry_condition_review']['conditions'] = [FUTURE]
        self.assertEqual(entry_condition_errors(plan), [])  # Structural claims alone pass no execution authority.
        self.seal(plan, independent=False)
        result = self.decide(plan)
        self.assertNotEqual(result['status'], 'actionable')
        self.assertEqual(self.store.items('reservation'), [])

    def test_fundamental_mode_remains_conditional_even_with_complete_cash_and_true_boolean(self):
        plan = approved_plan('CRCL')
        plan['entry'].update(basis='independent_valuation', condition_mode='price_and_fundamental', conditions=[FUTURE])
        plan['entry_condition_review'].update(condition_mode='price_and_fundamental',
            all_text_conditions_covered=False, conditions=[FUTURE])
        self.seal(plan)
        classification = classify_plan_validation(plan)
        self.assertEqual(classification['research_errors'], [])
        self.assertIn('entry_conditions_unverified', classification['execution_gaps'])
        result = self.decide(plan)
        self.assertNotEqual(result['status'], 'actionable')

    def test_all_text_conditions_and_exact_supported_price_bounds_must_be_independently_covered(self):
        for case in ('uncovered_text', 'different_boundary', 'unsupported_predicate'):
            with self.subTest(case=case):
                # Each case also retains a valid independent source seal, so a
                # rejection cannot be explained by missing provenance or cash.
                plan = approved_plan('CRCL')
                if case == 'uncovered_text':
                    plan['entry']['conditions'].append(FUTURE)
                elif case == 'different_boundary':
                    plan['entry']['execution_conditions'][0]['high'] = '200'
                    plan['entry_condition_review']['execution_conditions'] = copy.deepcopy(plan['entry']['execution_conditions'])
                else:
                    plan['entry']['execution_conditions'] = [{'type': 'future_earnings_confirmed', 'low': '90', 'high': '110'}]
                    plan['entry_condition_review']['execution_conditions'] = copy.deepcopy(plan['entry']['execution_conditions'])
                self.assertEqual(entry_condition_errors(plan), ['entry_conditions_unverified'])
                self.seal(plan)
                result = self.decide(plan)
                self.assertNotEqual(result['status'], 'actionable')

    def test_complete_independent_price_contract_produces_one_intent_and_only_next_quote_paper_fill(self):
        plan = approved_plan('CRCL')
        self.seal(plan)
        paper_account(self.store)
        signal = self.decide(plan, account='paper')
        self.assertEqual((signal['action'], signal['status']), ('buy', 'actionable'))
        same_tick = simulate(self.store, signal, [quote(NOW, symbol='CRCL')], now=NOW)
        self.assertEqual(same_tick['status'], 'skipped')
        later = (parse_time(NOW) + timedelta(seconds=1)).isoformat()
        filled = simulate(self.store, signal, [quote(later, symbol='CRCL')], now=later)
        self.assertEqual(filled['status'], 'filled')
        repeated = self.decide(plan, account='paper', now=later)
        self.assertNotEqual(repeated['status'], 'actionable')
        self.assertEqual(len(self.store.items('advice_intent')), 1)
        self.assertEqual(len(self.store.items('paper_fill')), 1)
        self.assertEqual(len([r for r in self.store.events('ledger') if r['account'] == 'real' and r['type'] == 'buy']), 0)

    def test_legacy_entry_gap_does_not_silence_verified_price_stop_or_rearm_duplicate_exit(self):
        plan = approved_plan('CRCL')
        plan['entry'].pop('condition_mode')
        plan['entry'].pop('execution_conditions')
        plan.pop('entry_condition_review')
        plan['stop']['confirmation'] = 'intraday'
        self.seal(plan, independent=False)
        self.record('position', 'opening_position', symbol='CRCL', quantity='10', price='100', fee='0')
        first = self.decide(plan, price='70')
        again = self.decide(plan, now='2026-10-23T15:00:01Z', price='71')
        self.assertEqual((first['action'], first['status'], first['quantity']), ('exit', 'actionable', '10'))
        self.assertEqual((again['action'], again['status'], again['id']), ('exit', 'actionable', first['id']))
        self.assertIn('confirmed_price_stop', first['reasons'])
        self.assertEqual(len(self.store.items('advice_intent')), 1)
        self.assertEqual(self.store.items('advice_intent')[0][1]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
