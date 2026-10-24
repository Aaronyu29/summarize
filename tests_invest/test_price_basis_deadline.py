"""Safety behavior at an issuer-announced change of absolute-price units."""
import copy
import hashlib
import unittest
from datetime import timedelta
from unittest.mock import patch

from rhino_invest.common import parse_time
from rhino_invest.decisions import evaluate, record_thesis_risk
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio
from rhino_invest.research import _price_basis_deadline
from tests_invest.plan_fixtures import seal_fixture_review as seal_plan_review
from rhino_invest.store import Store
from tests_invest.test_acceptance_core import approved_plan, quote


class PriceBasisDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.store.put('research_evidence', 'sealed:1', {'id': 'sealed:1'}, immutable=True)
        self.plan = approved_plan('SOXX')
        self.plan['price_basis_valid_until'] = '2026-11-05T05:00:00+00:00'
        seal_plan_review(self.store, self.plan, {'accepted': True}, self.plan['evidence_refs'])
        self.store.put('plan', self.plan['id'], self.plan)
        self.store.put('account_control', 'real', {'confirmed': True, 'holdings_complete': True})
        self.fill('cash', 'opening_cash', amount='10000')

    def fill(self, identity, kind, **fields):
        stage_entry(self.store, {'id': identity, 'type': kind, 'account': 'real', 'currency': 'USD',
            'timestamp': '2026-10-23T15:00:00Z', **fields})
        confirm_entry(self.store, identity)

    def decide(self, now, price='100', plan=None):
        snapshot = quote(now, price=price)
        snapshot['next_regular_close'] = (parse_time(now) + timedelta(hours=8)).isoformat()
        return evaluate(self.store, plan or self.plan, snapshot, portfolio(self.store, 'real', now=now), now=now)

    def test_before_effective_deadline_valid_plan_can_advise_but_after_deadline_cannot_reuse_absolute_prices(self):
        before = self.decide('2026-11-04T15:00:00Z')
        self.assertEqual((before['action'], before['status']), ('buy', 'actionable'))
        after = self.decide('2026-11-05T15:00:00Z')
        self.assertNotEqual(after['status'], 'actionable')
        self.assertIn('announced_corporate_action_requires_price_unit_review', after['reasons'])
        self.assertIsNone(after.get('quantity'))
        self.assertEqual(len(self.store.events('ledger')), 1)

    def test_expired_price_basis_cannot_use_old_take_profit_or_intraday_stop(self):
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        for price in ('70', '160'):
            with self.subTest(price=price):
                plan = copy.deepcopy(self.plan)
                plan['stop']['confirmation'] = 'intraday'
                seal_plan_review(self.store, plan, {'accepted': True}, plan['evidence_refs'])
                result = self.decide('2026-11-05T15:00:00Z', price, plan)
                self.assertNotEqual(result['status'], 'actionable')
                self.assertNotIn('confirmed_price_stop', result['reasons'])
                self.assertNotIn('take_profit_schedule_reached', result['reasons'])
                self.assertIsNone(result.get('quantity'))

    def test_verified_thesis_risk_survives_price_basis_expiry_without_quantified_old_unit_exit(self):
        self.fill('held', 'opening_position', symbol='SOXX', quantity='2', price='100', fee='0')
        self.store.put('review_event_evidence', 'new-fact', {'cashflow': 'failed'}, immutable=True)
        record_thesis_risk(self.store, self.plan,
            {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['new-fact'], 'rationale': 'raw fact'},
            {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['new-fact'], 'issues': []},
            now='2026-10-24T01:00:00Z')
        result = self.decide('2026-11-05T15:00:00Z')
        self.assertEqual(result['status'], 'warning')
        self.assertIn('verified_thesis_invalidation', result['reasons'])
        self.assertIn('announced_corporate_action_requires_price_unit_review', result['reasons'])
        self.assertIsNone(result.get('quantity'))

    def test_malformed_price_basis_deadline_fails_closed(self):
        plan = copy.deepcopy(self.plan)
        plan['price_basis_valid_until'] = 'not a date'
        seal_plan_review(self.store, plan, {'accepted': True}, plan['evidence_refs'])
        result = self.decide('2026-11-04T15:00:00Z', plan=plan)
        self.assertNotEqual(result['status'], 'actionable')
        self.assertIn('announced_corporate_action_requires_price_unit_review', result['reasons'])

    def issuer_action(self, effective='2026-11-05'):
        text = 'SOXX forward stock split. Shares of SOXX will begin trading on a split-adjusted basis on November 5, 2026.'
        self.store.put('external_evidence', 'issuer-split', {'content': text,
            'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'source_url': 'https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf'}, immutable=True)
        return {'etf_valuation': {'nav': {'as_of': '2026-10-22'}, 'corporate_actions': [{'type': 'split',
            'effective_at': effective, 'source_excerpt': text, 'evidence_refs': ['issuer-split']}]}}

    def test_verified_split_generates_new_york_midnight_deadline_not_utc_midnight(self):
        data = self.issuer_action()
        self.assertEqual(_price_basis_deadline(data, '2026-10-24T01:00:00Z', {'issuer-split'}, self.store),
                         '2026-11-05T05:00:00+00:00')

    def test_corporate_action_date_must_match_original_source_not_only_existing_excerpt(self):
        data = self.issuer_action(effective='2026-11-06')
        self.assertIsNone(_price_basis_deadline(data, '2026-10-24T01:00:00Z', {'issuer-split'}, self.store))


if __name__ == '__main__':
    unittest.main()
