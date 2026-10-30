import copy
import unittest

from rhino_invest.common import classify_plan_validation, validate_plan, validate_research_plan
from rhino_invest.decisions import evaluate
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio
from rhino_invest.reporting import plan_row, notification_text, actionable_notice
from rhino_invest.research import seal_plan_review
from rhino_invest.store import Store
from tests_invest.test_acceptance_core import approved_plan, quote


NOW = '2026-10-23T15:00:00Z'


def conditional_plan():
    plan = approved_plan('GOOG')
    plan['entry'].update(basis='independent_valuation', low=None, high=None,
        conditions=['Next official earnings release confirms operating growth without thesis invalidation'])
    plan['conditions_parsed'] = False
    return plan


class ConditionalResearchValidationTests(unittest.TestCase):
    def test_future_fundamental_condition_is_execution_gap_and_strict_validation_is_unchanged(self):
        plan = conditional_plan()
        before = copy.deepcopy(plan)
        strict = validate_plan(plan)
        classification = classify_plan_validation(plan)
        self.assertEqual(classification['research_errors'], [])
        self.assertEqual(set(classification['execution_gaps']), {
            'entry.low must be positive', 'entry.high must be positive', 'entry conditions are unparsed',
            'entry_conditions_unverified'})
        self.assertEqual(validate_research_plan(plan), [])
        self.assertEqual(validate_plan(plan), strict)
        self.assertEqual(plan, before)

    def test_numeric_errors_source_units_targets_and_stop_are_never_reclassified(self):
        for field, value, expected in (
            ('entry.low', '0', 'entry.low must be positive'),
            ('entry.high', '-1', 'entry.high must be positive'),
            ('entry.low', 'unknown', 'entry.low must be positive'),
            ('stop.price', None, 'stop.price must be positive'),
            ('price_unit', None, 'price_unit must be per_share/per_adr'),
            ('evidence_refs', [], 'evidence_refs required'),
            ('targets', [], 'evidenced target scenarios required')):
            with self.subTest(field=field, value=value):
                plan = conditional_plan()
                if '.' in field:
                    parent, name = field.split('.')
                    plan[parent][name] = value
                else:
                    plan[field] = value
                self.assertIn(expected, validate_research_plan(plan))

    def test_source_zone_missing_bounds_and_missing_conditions_remain_research_defects(self):
        for basis, conditions in (('source_level', ['wait for confirmation']),
                                  ('independent_valuation', []), ('independent_valuation', [None])):
            plan = conditional_plan()
            plan['entry'].update(basis=basis, conditions=conditions)
            errors = validate_research_plan(plan)
            if basis == 'source_level':
                # A source zone must retain its original numeric bounds even
                # when a non-price entry condition is pending independently.
                self.assertIn('entry.low must be positive', errors)
                self.assertIn('entry.high must be positive', errors)
            else:
                self.assertIn('entry conditions are unparsed', errors)
                self.assertEqual(classify_plan_validation(plan)['execution_gaps'], [])

    def test_conditional_research_approval_does_not_create_trade_or_reserve_cash(self):
        with Store(':memory:') as store:
            plan = conditional_plan()
            plan['research_assessment'] = {'status': 'approved_conditional'}
            store.put('research_evidence', 'sealed:1', {'id': 'sealed:1'}, immutable=True)
            seal_plan_review(store, plan, {'accepted': True}, plan['evidence_refs'])
            store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
            stage_entry(store, {'id': 'cash', 'account': 'real', 'currency': 'USD', 'type': 'opening_cash',
                'amount': '10000', 'timestamp': '2026-10-22T15:00:00Z'})
            confirm_entry(store, 'cash')
            advice = evaluate(store, plan, quote(NOW, symbol='GOOG'), portfolio(store, 'real', now=NOW), now=NOW)
            self.assertNotEqual(advice['status'], 'actionable')
            self.assertIsNone(advice.get('quantity'))
            self.assertEqual(store.items('reservation'), [])
            self.assertEqual(store.items('paper_fill'), [])

    def test_user_projection_says_valuation_reviewed_without_showing_buy_prices_or_action(self):
        plan = conditional_plan()
        plan.update(status='needs_review', research_assessment={'status': 'approved_conditional'})
        row = plan_row(plan, {'action': 'buy', 'status': 'actionable'})
        self.assertEqual(row['状态'], '估值已审核·入场条件待满足')
        self.assertEqual(row['建议动作'], '等待入场条件')
        self.assertEqual(row['目标情景'], '待审核，暂不展示')
        text = notification_text({'kind': '每日复盘', 'timestamp': NOW, 'portfolio': {'cash': '10000', 'positions': []}},
            current_plans=[plan], now=NOW)
        self.assertIn('1项估值已审核，入场条件待满足', text)
        self.assertNotIn('1项研究待核实', text)
        self.assertNotIn('$150', text)
        recommendation = {'plan_id': plan['id'], 'revision': 1, 'action': 'buy', 'status': 'actionable',
            'expires_at': '2026-10-23T20:00:00Z'}
        self.assertFalse(actionable_notice(recommendation, [dict(plan, status='active')], now=NOW))

    def test_rejected_or_invalidated_research_is_not_labelled_conditionally_approved(self):
        plan = conditional_plan()
        plan.update(status='needs_review', research_assessment={'status': 'needs_evidence'})
        self.assertEqual(plan_row(plan)['状态'], '已自动审核·未通过')
        plan.update(status='invalidated', research_assessment={'status': 'approved_conditional'})
        self.assertEqual(plan_row(plan)['状态'], '已失效')


if __name__ == '__main__':
    unittest.main()
