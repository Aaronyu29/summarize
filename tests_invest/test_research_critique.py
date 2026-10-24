"""A rejected draft may receive one critique, never an unbounded model loop."""
import copy
import json
import unittest

from rhino_invest.common import stable_id
from rhino_invest.model import ModelGateway
from rhino_invest.research import ingest_archives, _calculate_target_prices
from tests_invest import test_research_evidence_contract as evidence_tests
from tests_invest.test_research import NOW
from tests_invest.test_research_evidence_contract import IndependentModel


class CritiqueModel(IndependentModel):
    def __init__(self, always_reject=False):
        super().__init__()
        self.always_reject = always_reject

    def run(self, purpose, payload, **kwargs):
        result = super().run(purpose, payload, **kwargs)
        if purpose == 'compile_plan' and ((not payload.get('critique') and not payload['fundamentals'].get('force_accept')) or self.always_reject):
            result['plan']['take_profit'][0]['fraction'] = None
        if purpose == 'compile_plan' and payload['fundamentals'].get('conditional'):
            result['plan']['entry'].update(low=None, high=None, conditions=['Await confirmed issuer financial results'],
                condition_mode='price_and_fundamental', execution_conditions=[])
            result['plan']['conditions_parsed'] = False
            result['plan']['take_profit'][0]['fraction'] = '.5'
        if purpose == 'review_plan':
            valid = payload['plan']['take_profit'][0]['fraction'] is not None
            result.update(accepted=valid, issues=[] if valid else [{
                'code': 'TAKE_PROFIT_FRACTION_MISSING', 'detail': 'Authored schedule needs explicit fractions, not invented holdings.'}])
        return result


class CritiqueTests(unittest.TestCase):
    setUp = evidence_tests.EvidenceContractTests.setUp

    def facts(self):
        data, _ = evidence_tests.EvidenceContractTests.primary(self)
        data['company_profile'] = {'company_name': 'Fixture legal operating company'}
        return data

    def ingest(self, model, data, now=NOW):
        return ingest_archives(self.store, self.db, model=model, fundamentals={'NVDA': data}, now=now)

    def gateway(self, transport, *, clock=None, daily=20):
        return ModelGateway(self.store, {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': daily, 'max_cost_usd_per_call': '.1', 'max_cost_usd_per_day': str(daily / 10)},
            transport, clock=clock or (lambda: NOW))

    def provider(self, fixture, calls, *, failure=None):
        def run(body):
            task = json.loads(body['messages'][1]['content'][0]['text'])
            purpose, payload = task['purpose'], task['evidence']
            semantic = bool(payload.get('critique') or payload.get('critique_context'))
            calls.append((purpose, semantic))
            if failure:
                response = failure(purpose, payload, body)
                if response is not None:
                    return response
            result = fixture.run(purpose, payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        return run

    def test_cached_rejection_gets_one_frozen_critique_then_independent_acceptance(self):
        data, model = self.facts(), CritiqueModel()
        old = self.ingest(model, data)['plans'][0]
        old_binding = self.store.get('research_plan_review', old['review_evidence_id'])
        self.assertEqual(model.calls, ['extract_levels', 'verify_levels', 'compile_plan', 'review_plan'])
        result = self.ingest(model, data)
        self.assertEqual(result['errors'], [])
        new = result['plans'][0]
        self.assertEqual(new['status'], 'active', new['review_issues'])
        self.assertEqual(model.calls[-2:], ['compile_plan', 'review_plan'])
        self.assertEqual(new['research_assessment']['repair_status'], 'critique_accepted')
        self.assertEqual(len(self.store.items('research_critique_attempt')), 1)
        request = next(payload for purpose, payload in model.payloads if purpose == 'compile_plan' and payload.get('critique'))
        self.assertIsNone(request['critique']['economic_proposal']['take_profit'][0]['fraction'])
        self.assertTrue(request['critique']['review_is_untrusted_opinion'])
        self.assertNotIn('verification', request['critique']['economic_proposal'])
        for day in (24, 25, 26):
            replay = self.ingest(model, dict(data, observed_at='irrelevant'), f'2026-10-{day}T15:00:00+00:00')
            self.assertEqual(replay['plans'][0], new)
        self.assertEqual(len(model.calls), 6)
        self.assertEqual(self.store.get('research_plan_review', old['review_evidence_id']), old_binding)
        self.assertEqual(self.store.get('plan_revision', stable_id(old['id'], old['revision'])), old)

    def test_again_rejected_is_terminal_across_ticks_and_days(self):
        model, data = CritiqueModel(always_reject=True), self.facts()
        self.ingest(model, data)
        repaired = self.ingest(model, data)['plans'][0]
        self.assertEqual(repaired['status'], 'needs_review')
        self.assertEqual(repaired['research_assessment']['repair_status'], 'critique_exhausted')
        for day in (24, 25, 26):
            self.ingest(model, data, f'2026-10-{day}T15:00:00+00:00')
        self.assertEqual(len(model.calls), 6)
        self.assertEqual(len(self.store.items('research_critique_attempt')), 1)

    def test_old_rejection_cannot_replace_new_approved_or_conditional_thesis(self):
        for conditional in (False, True):
            with self.subTest(conditional=conditional):
                data = self.facts()
                data['fixture_cycle'] = str(conditional)
                model = CritiqueModel()
                self.ingest(model, data)
                newer = dict(data, conditional=conditional, force_accept=True)
                current = self.ingest(model, newer)['plans'][0]
                self.assertEqual(current['research_assessment']['status'], 'approved_conditional' if conditional else 'approved')
                count = len(model.calls)
                self.ingest(model, data)
                self.assertEqual(self.store.get('plan', current['id']), current)
                self.assertEqual(len(model.calls), count)

    def test_missing_unit_evidence_does_not_buy_a_semantic_repair(self):
        data, model = self.facts(), CritiqueModel()
        data['primary_financials']['instrument'] = {}
        self.ingest(model, data)
        self.ingest(model, data)
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(self.store.items('research_critique_attempt'), [])

    def test_unknown_child_stays_fenced_after_restart_and_day_change(self):
        data, calls, clock = self.facts(), [], [NOW]
        def fail(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                raise TimeoutError()
        transport = self.provider(CritiqueModel(), calls, failure=fail)
        model = self.gateway(transport, clock=lambda: clock[0])
        self.ingest(model, data)
        first = self.ingest(model, data)
        self.assertEqual(first['errors'][0]['error'], 'UnknownModelOutcome')
        clock[0] = '2026-10-24T15:00:00+00:00'
        restarted = self.gateway(transport, clock=lambda: clock[0])
        for _ in range(3):
            result = self.ingest(restarted, data, clock[0])
            self.assertEqual(result['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(len(calls), 5)

    def test_budget_deferral_continues_only_unfinished_phase_next_day(self):
        data, calls, clock = self.facts(), [], [NOW]
        transport = self.provider(CritiqueModel(), calls)
        self.ingest(self.gateway(transport, daily=4, clock=lambda: clock[0]), data)
        deferred = self.ingest(self.gateway(transport, daily=4, clock=lambda: clock[0]), data)
        self.assertEqual(deferred['errors'][0]['error'], 'ModelBudgetExceeded')
        self.assertEqual(len(calls), 4)
        clock[0] = '2026-10-24T15:00:00+00:00'
        middle = self.ingest(self.gateway(transport, daily=1, clock=lambda: clock[0]), data, clock[0])
        self.assertEqual(middle['errors'][0]['error'], 'ModelBudgetExceeded')
        self.assertEqual(len(self.store.items('research_critique_draft')), 1)
        clock[0] = '2026-10-25T15:00:00+00:00'
        final = self.ingest(self.gateway(transport, daily=1, clock=lambda: clock[0]), data, clock[0])
        self.assertEqual(final['errors'], [])
        self.assertEqual(final['plans'][0]['status'], 'active')
        self.assertEqual(calls, [(p, False) for p in ('extract_levels', 'verify_levels', 'compile_plan', 'review_plan')]
            + [('compile_plan', True), ('review_plan', True)])

    def test_known_double_bad_draft_is_terminal_not_reissued(self):
        data, calls = self.facts(), []
        def fail(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{bad json'}}]}
        model = self.gateway(self.provider(CritiqueModel(), calls, failure=fail))
        self.ingest(model, data)
        result = self.ingest(model, data)
        self.assertEqual(result['errors'][0]['error'], 'ModelOutputInvalid')
        self.assertEqual(len(calls), 6)
        for _ in range(3):
            result = self.ingest(model, data)
            self.assertEqual(result['errors'], [])
        self.assertEqual(len(calls), 6)
        self.assertEqual(self.store.items('research_critique_result')[0][1]['status'], 'output_invalid')

    def test_precise_etf_intermediates_and_explicit_eps_bridge_never_parse_prose(self):
        proposal = {'targets': [{'scenario': 'base', 'method': 'etf_nav_scenario', 'horizon_months': 24,
            'price': '609', 'assumptions': {'nav_per_share': '475.335589', 'current_pe': '44.96',
                'annual_earnings_growth': '.12', 'years': '2', 'exit_pe': '42'}}], 'take_profit': [{'price': '609', 'fraction': '.5'}]}
        plan, book = _calculate_target_prices(proposal)
        row = book['targets'][0]
        self.assertEqual(plan['targets'][0]['price'], '557.01')
        self.assertEqual(row['intermediates']['growth_factor'], '1.2544')
        self.assertTrue(row['intermediates']['implied_earnings_proxy_per_etf_share'].startswith('10.572410787366548'))
        self.assertTrue(row['unrounded_price'].startswith('557.005347850249'))
        target = {'method': 'eps_multiple', 'price': '703.5', 'assumptions': {'eps': '23.45', 'multiple': '30',
            'rationale': '16.5 * (1.24)^2. This prose is not calculator input.'}}
        unchanged, _ = _calculate_target_prices({'targets': [target]})
        self.assertEqual(unchanged['targets'][0]['assumptions']['eps'], '23.45')
        target['assumptions']['eps_bridge'] = {'anchor_eps': '16.5', 'annual_growth': '.24', 'years': '2'}
        corrected, record = _calculate_target_prices({'targets': [target], 'take_profit': [{'price': '703.5', 'fraction': '.5'}]})
        self.assertEqual(corrected['targets'][0]['assumptions']['eps'], '25.37040')
        self.assertEqual(corrected['targets'][0]['price'], '761.11')
        self.assertEqual(corrected['take_profit'][0]['price'], '761.11')
        self.assertEqual(record['targets'][0]['eps_bridge']['original_eps'], '23.45')

    def test_completed_null_plan_is_terminal_schema_invalid_across_days(self):
        data, calls = self.facts(), []
        def fail(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"plan":null}'}}]}
        transport = self.provider(CritiqueModel(), calls, failure=fail)
        old = self.ingest(self.gateway(transport), data)['plans'][0]
        for day in (23, 24, 25):
            result = self.ingest(self.gateway(transport), data, f'2026-10-{day}T15:00:00+00:00')
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['plans'][0], old)
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.store.items('research_critique_result')[0][1]['status'], 'schema_invalid')
        self.assertIsNone(self.store.items('research_critique_draft')[0][1]['proposal'])
        self.assertEqual(len(self.store.items('model_receipt')), 5)

    def test_review_recovery_checkpoint_cannot_rearm_after_new_revision(self):
        data, calls, clock = self.facts(), [], [NOW]
        def fail(purpose, payload, body):
            system = body['messages'][0]['content']
            if purpose == 'compile_plan' and payload.get('critique') and 'FORMAT_REGENERATION_ATTEMPT=compile-json-v1' not in system:
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{bad json'}}]}
            if purpose == 'review_plan' and payload.get('critique_context') and 'REVIEW_JSON_RECOVERY_ATTEMPT=1' not in system:
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"accepted":true}}'}}]}
        transport = self.provider(CritiqueModel(), calls, failure=fail)
        def ingest(daily):
            return self.ingest(self.gateway(transport, daily=daily, clock=lambda: clock[0]), data, clock[0])
        ingest(4)
        clock[0] = '2026-10-24T15:00:00+00:00'
        self.assertEqual(ingest(2)['errors'][0]['error'], 'ModelBudgetExceeded')
        clock[0] = '2026-10-25T15:00:00+00:00'
        self.assertEqual(ingest(1)['errors'][0]['error'], 'ModelBudgetExceeded')
        checkpoint = copy.deepcopy(self.store.items('research_critique_review_input')[0][1])
        key, current = self.store.items('plan')[0]
        current = dict(current, revision=current['revision'] + 1)
        self.store.put('plan', key, current)
        clock[0] = '2026-10-26T15:00:00+00:00'
        self.assertEqual(ingest(2)['errors'], [])
        self.assertEqual(sum(semantic for _, semantic in calls), 3)
        self.assertEqual(self.store.items('research_critique_result')[0][1]['status'], 'superseded')
        self.assertEqual(self.store.get('plan', key), current)
        self.assertEqual(self.store.items('research_critique_review_input')[0][1], checkpoint)

    def test_nested_unhashable_evidence_references_are_terminal_schema_invalid(self):
        data, calls = self.facts(), []
        fixture = CritiqueModel()
        def fail(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                proposal = fixture.run(purpose, payload)
                proposal['plan']['targets'][0]['assumptions']['evidence_refs'] = [{}]
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(proposal)}}]}
        transport = self.provider(fixture, calls, failure=fail)
        self.ingest(self.gateway(transport), data)
        for day in (23, 24):
            self.assertEqual(self.ingest(self.gateway(transport), data,
                f'2026-10-{day}T15:00:00+00:00')['errors'], [])
        self.assertEqual(len(calls), 5)
        self.assertIn('array of strings', self.store.items('research_critique_result')[0][1]['issues'][0])

    def test_completed_invalid_stop_type_is_terminal_after_independent_review(self):
        data, calls = self.facts(), []
        fixture = CritiqueModel()
        def fail(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                proposal = fixture.run(purpose, payload)
                proposal['plan']['stop']['confirmation'] = ['daily_close']
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(proposal)}}]}
        transport = self.provider(fixture, calls, failure=fail)
        old = self.ingest(self.gateway(transport), data)['plans'][0]
        for day in (23, 24, 25):
            result = self.ingest(self.gateway(transport), data, f'2026-10-{day}T15:00:00+00:00')
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['plans'][0], old)
            self.assertEqual(self.store.get('plan', old['id']), old)
        terminal = self.store.items('research_critique_result')[0][1]
        self.assertEqual((terminal['status'], terminal['phase'], terminal['error_type']),
            ('schema_invalid', 'assessment', 'TypeError'))
        draft = self.store.items('research_critique_draft')[0][1]
        review = self.store.items('research_critique_review')[0][1]
        self.assertEqual(terminal['proposal_hash'], draft['proposal_hash'])
        self.assertEqual(terminal['review_hash'], review['review_hash'])
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(self.store.items('model_receipt')), 6)

    def test_new_plan_committed_during_review_is_not_overwritten(self):
        data, calls, concurrent = self.facts(), [], []
        def replace(purpose, payload, body):
            if purpose == 'review_plan' and payload.get('critique_context'):
                key, old = self.store.items('plan')[0]
                newer = dict(old, revision=old['revision'] + 1, status='active', thesis='Concurrently reviewed thesis')
                self.store.put('plan', key, newer)
                concurrent.append(newer)
        transport = self.provider(CritiqueModel(), calls, failure=replace)
        self.ingest(self.gateway(transport), data)
        result = self.ingest(self.gateway(transport), data)
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.store.get('plan', concurrent[0]['id']), concurrent[0])
        self.assertEqual(result['plans'][0], concurrent[0])
        self.assertEqual(self.store.items('research_critique_result')[0][1]['status'], 'superseded')

    def test_both_format_recoveries_use_at_most_four_semantic_requests(self):
        data, calls, clock = self.facts(), [], [NOW]
        def fail(purpose, payload, body):
            system = body['messages'][0]['content']
            if purpose == 'compile_plan' and payload.get('critique') and 'FORMAT_REGENERATION_ATTEMPT=compile-json-v1' not in system:
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{bad json'}}]}
            if purpose == 'review_plan' and payload.get('critique_context') and 'REVIEW_JSON_RECOVERY_ATTEMPT=1' not in system:
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{bad json'}}]}
        transport = self.provider(CritiqueModel(), calls, failure=fail)
        self.ingest(self.gateway(transport, clock=lambda: clock[0]), data)
        for day, budget in ((24, 2), (25, 1), (26, 1)):
            clock[0] = f'2026-10-{day}T15:00:00+00:00'
            result = self.ingest(self.gateway(transport, daily=budget, clock=lambda: clock[0]), data, clock[0])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['plans'][0]['status'], 'active')
        self.assertEqual(sum(semantic for _, semantic in calls), 4)
        self.assertEqual(len(self.store.items('model_receipt')), 8)
        clock[0] = '2026-10-27T15:00:00+00:00'
        self.ingest(self.gateway(transport, clock=lambda: clock[0]), data, clock[0])
        self.assertEqual(sum(semantic for _, semantic in calls), 4)


if __name__ == '__main__':
    unittest.main()
