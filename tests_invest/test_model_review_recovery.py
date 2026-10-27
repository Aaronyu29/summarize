import copy
import json
import unittest

from rhino_invest.common import stable_id
from rhino_invest.model import (ModelGateway, ModelBudgetExceeded, ModelOutputInvalid,
    ModelRejected, UnknownModelOutcome)
from rhino_invest.store import Store

NOW = '2026-10-24T05:00:00+00:00'
CONFIG = {'enabled': True, 'model': 'offline-fixture', 'base_url': 'https://model.invalid/v1',
          'max_cost_usd_per_call': '0.1', 'max_cost_usd_per_day': '0.3', 'max_calls_per_day': 3}
PAYLOAD = {'plan': {'id': 'p', 'evidence_refs': ['issuer-raw'], 'targets': ['authored-scenario']},
           'fundamentals': {'original': 'same official evidence'}}
BAD = '{"accepted":true,"issues":[]}}'
REVIEW = {'accepted': False, 'issues': ['Unresolved original evidence'],
          'checks': {'provenance': True, 'currency': True, 'unit': False, 'adr': True}}


def response(content=BAD, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': content}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.calls = []

    def model(self, replies, config=None):
        def transport(body):
            self.calls.append(copy.deepcopy(body))
            item = replies.pop(0)
            if isinstance(item, Exception): raise item
            return item
        return ModelGateway(self.store, config or CONFIG, transport, clock=lambda: NOW)

    def run_review(self, model):
        return model.run('review_plan', PAYLOAD, instruction='Independently review original sources.',
                         call_scope='original-review-scope', now=NOW)

    def test_one_independent_review_preserves_original_and_does_not_force_approval(self):
        raw_response = response()
        result = self.run_review(self.model([raw_response, response(json.dumps(REVIEW))]))
        self.assertEqual(result, REVIEW)
        self.assertFalse(result['accepted'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]['messages'][1], self.calls[1]['messages'][1])
        self.assertNotIn(BAD, json.dumps(self.calls[1]))
        self.assertIn('REVIEW_JSON_RECOVERY_ATTEMPT=1', self.calls[1]['messages'][0]['content'])
        audit = self.store.items('model_recovery')[0][1]
        parent = self.store.get('model_receipt', audit['parent_call_id'])
        self.assertEqual(parent['response'], raw_response)
        self.assertEqual(audit['parent_receipt_hash'], stable_id(parent))
        child = self.store.get('model_call', audit['id'])
        self.assertEqual(child['call_scope'], self.store.get('model_call', audit['parent_call_id'])['call_scope'])
        self.assertEqual(child['recovery_parent_id'], audit['parent_call_id'])
        self.assertEqual(self.store.get('model_receipt', audit['id'])['recovery_parent_id'], audit['parent_call_id'])
        self.assertIsNone(self.store.get('model_response', audit['parent_call_id']))

    def test_legacy_cached_invalid_recovers_once_across_gateway_restarts(self):
        old = self.model([response()])
        with self.assertRaises(ModelOutputInvalid):
            old._run_once('review_plan', PAYLOAD, now=NOW, instruction='Independently review original sources.',
                          call_scope='original-review-scope')
        self.assertEqual(self.store.items('model_recovery'), [])
        self.assertEqual(self.run_review(self.model([response(json.dumps(REVIEW))])), REVIEW)
        self.assertEqual(self.run_review(self.model([])), REVIEW)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.store.items('model_call')), 2)

    def test_double_malformed_is_bounded_after_restart_and_new_day(self):
        with self.assertRaises(ModelOutputInvalid):
            self.run_review(self.model([response(), response()]))
        model = self.model([])
        model.clock = lambda: '2026-10-25T05:00:00+00:00'
        with self.assertRaises(ModelOutputInvalid): self.run_review(model)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.store.items('model_receipt')), 2)

    def test_budget_blocks_recovery_then_permits_only_one_reserved_child(self):
        limited = dict(CONFIG, max_calls_per_day=1)
        with self.assertRaises(ModelBudgetExceeded): self.run_review(self.model([response()], limited))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store.items('model_call')), 1)
        self.assertEqual(self.run_review(self.model([response(json.dumps(REVIEW))])), REVIEW)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(sum(float(v['reserved_cost_usd']) for _, v in self.store.items('model_call')), .2)

    def test_daily_cost_cap_blocks_child_without_paid_call(self):
        with self.assertRaises(ModelBudgetExceeded):
            self.run_review(self.model([response()], dict(CONFIG, max_cost_usd_per_day='.1')))
        self.assertEqual(len(self.calls), 1)

    def test_child_unknown_cannot_be_bypassed_by_restart_payload_or_config(self):
        with self.assertRaises(UnknownModelOutcome):
            self.run_review(self.model([response(), TimeoutError()]))
        with self.assertRaises(UnknownModelOutcome): self.run_review(self.model([]))
        changed = self.model([], dict(CONFIG, configuration_revision=900, max_tokens=16000))
        with self.assertRaises(UnknownModelOutcome):
            changed.run('review_plan', {'different': 'layout'}, call_scope='original-review-scope')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.store.items('model_receipt')), 1)

    def test_parent_unknown_never_gets_recovery(self):
        with self.assertRaises(UnknownModelOutcome): self.run_review(self.model([TimeoutError()]))
        with self.assertRaises(UnknownModelOutcome): self.run_review(self.model([]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.items('model_recovery'), [])

    def test_truncated_nontext_valid_list_and_schema_defects_do_not_retry(self):
        cases = [response('{}', 'length'), response(None), response('[]'), response('null')]
        for provider in cases:
            with self.subTest(provider=provider), Store(':memory:') as store:
                calls = []
                model = ModelGateway(store, CONFIG, lambda body: calls.append(body) or provider)
                with self.assertRaises(ModelOutputInvalid): model.run('review_plan', PAYLOAD)
                with self.assertRaises(ModelOutputInvalid): model.run('review_plan', PAYLOAD)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.items('model_recovery'), [])

    def test_compile_and_event_reviews_are_not_recovered_by_gateway(self):
        for purpose in ('compile_plan', 'event_review'):
            with self.subTest(purpose=purpose), Store(':memory:') as store:
                calls = []
                model = ModelGateway(store, CONFIG, lambda body: calls.append(body) or response())
                with self.assertRaises(ModelOutputInvalid): model.run(purpose, PAYLOAD)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.items('model_recovery'), [])

    def test_child_requires_minimal_schema_and_no_third_attempt(self):
        with self.assertRaises(ModelOutputInvalid):
            self.run_review(self.model([response(), response('{"accepted":true}')]))
        with self.assertRaises(ModelOutputInvalid): self.run_review(self.model([]))
        self.assertEqual(len(self.calls), 2)

    def test_old_malformed_parent_does_not_bypass_other_unknown_same_scope(self):
        with self.assertRaises(ModelOutputInvalid):
            self.model([response()])._run_once('review_plan', PAYLOAD, now=NOW,
                instruction='Independently review original sources.', call_scope='original-review-scope')
        original = self.store.items('model_call')[0][1]
        self.store.put('model_call', 'same-scope-unknown', dict(original, id='same-scope-unknown'), immutable=True)
        with self.assertRaises(UnknownModelOutcome): self.run_review(self.model([]))
        self.assertEqual(len(self.calls), 1)

    def test_recovered_cache_does_not_hide_a_later_unresolved_same_scope_call(self):
        self.run_review(self.model([response(), response(json.dumps(REVIEW))]))
        audit = self.store.items('model_recovery')[0][1]
        original = self.store.get('model_call', audit['parent_call_id'])
        self.store.put('model_call', 'later-unknown', dict(original, id='later-unknown'), immutable=True)
        with self.assertRaises(UnknownModelOutcome): self.run_review(self.model([]))
        self.assertEqual(len(self.calls), 2)

    def test_recovery_preserves_original_evidence_and_independent_entry_condition_review(self):
        payload = copy.deepcopy(PAYLOAD)
        payload['plan']['entry'] = {'low': '90', 'high': '110',
            'conditions': ['Enter only when the official execution price is between 90 and 110 USD.'],
            'condition_mode': 'price_only',
            'execution_conditions': [{'type': 'price_between', 'low': '90', 'high': '110'}]}
        original = copy.deepcopy(payload)
        independent = {'condition_mode': 'price_only', 'all_text_conditions_covered': True,
            'conditions': payload['plan']['entry']['conditions'],
            'execution_conditions': payload['plan']['entry']['execution_conditions']}
        review = dict(REVIEW, accepted=True, issues=[], entry_condition_review=independent)
        model = self.model([response(), response(json.dumps(review))])
        result = model.run('review_plan', payload, instruction='Independently verify the original issuer evidence.',
                           call_scope='original-entry-contract', now=NOW)
        self.assertEqual(result['entry_condition_review'], independent)
        self.assertEqual(payload, original)
        for body in self.calls:
            sent = json.loads(body['messages'][1]['content'][0]['text'])
            self.assertEqual(sent['evidence'], original)
        instruction = self.calls[1]['messages'][0]['content']
        self.assertIn('independently return entry_condition_review', instruction)
        self.assertIn('every original condition', instruction)
        self.assertIn('price_and_fundamental with coverage false', instruction)


if __name__ == '__main__':
    unittest.main()
