import copy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rhino_invest.common import stable_id
from rhino_invest.research import _evidence_bundle, _operation_evidence_bundle, compile_plan
from rhino_invest.store import Store
from tests_invest import test_research_critique as critique
from tests_invest.test_research import NOW


class EvidenceOrderTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        for ref in ('a', 'b'):
            self.store.put('external_evidence', ref, {'id': ref, 'content': ref,
                'sha256': hashlib.sha256(ref.encode()).hexdigest()}, immutable=True)

    def test_restart_uses_frozen_order_and_identical_bundle(self):
        first = _operation_evidence_bundle(self.store, 'op', ['b', 'a'], 'fid')
        restarted = _operation_evidence_bundle(self.store, 'op', ['a', 'b'], 'fid')
        self.assertEqual(first, restarted)

    def test_real_process_restart_with_different_hash_seed_keeps_bundle(self):
        script = '''import json,sys
from rhino_invest.store import Store
from rhino_invest.research import _operation_evidence_bundle
with Store(sys.argv[1]) as store:
 print(json.dumps(_operation_evidence_bundle(store,'op',list({'a','b'}),'fid'),sort_keys=True))
'''
        with tempfile.TemporaryDirectory(prefix='.evidence-order-', dir=Path.cwd()) as directory:
            path = Path(directory)/'state.sqlite'
            with Store(path) as target:
                self.store.db.backup(target.db)
            outputs = [subprocess.check_output([sys.executable, '-c', script, str(path)],
                env=dict(os.environ, PYTHONHASHSEED=seed), text=True) for seed in ('1', '3')]
        self.assertEqual(outputs[0], outputs[1])

    def test_legacy_repair_restores_exact_hash_without_editing_original(self):
        old = _evidence_bundle(self.store, ['b', 'a'], 'fid')
        record = {'evidence_bundle_hash': old['hash'], 'status': 'original'}
        self.store.put('research_repair', 'op', record, immutable=True)
        restored = _operation_evidence_bundle(self.store, 'op', ['a', 'b'], 'fid')
        self.assertEqual(restored, old)
        self.assertEqual(self.store.get('research_repair', 'op'), record)
        self.assertEqual(_operation_evidence_bundle(self.store, 'op', ['a', 'b'], 'fid'), old)

    def test_real_content_or_membership_change_never_becomes_order_repair(self):
        _operation_evidence_bundle(self.store, 'op', ['a', 'b'], 'fid')
        with self.assertRaisesRegex(ValueError, 'membership'):
            _operation_evidence_bundle(self.store, 'op', ['a'], 'fid')
        original = self.store.get
        def changed(kind, key):
            value = original(kind, key)
            return dict(value, as_of='changed') if kind == 'external_evidence' and key == 'b' else value
        with patch.object(self.store, 'get', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'content changed'):
                _operation_evidence_bundle(self.store, 'op', ['a', 'b'], 'fid')
        self.store.put('research_repair', 'legacy', {'evidence_bundle_hash': 'not-the-old-hash'}, immutable=True)
        with self.assertRaisesRegex(ValueError, 'differs from its sealed hash'):
            _operation_evidence_bundle(self.store, 'legacy', ['a', 'b'], 'fid')
        self.assertIsNone(self.store.get('plan_evidence_order', 'legacy'))


class LegacyResearchResumeTests(unittest.TestCase):
    setUp = critique.CritiqueTests.setUp
    facts = critique.CritiqueTests.facts
    ingest = critique.CritiqueTests.ingest
    gateway = critique.CritiqueTests.gateway
    provider = critique.CritiqueTests.provider

    def data_with_two_sources(self):
        data = self.facts()
        ref = stable_id('second-primary')
        self.store.put('external_evidence', ref, {'id': ref, 'content': 'second',
            'sha256': hashlib.sha256(b'second').hexdigest()}, immutable=True)
        data['primary_evidence'].append({'id': ref})
        return data

    @staticmethod
    def old_order(store, operation, refs, fid):
        external = [ref for ref in refs if store.get('external_evidence', ref)]
        rest = [ref for ref in refs if ref not in external]
        return _evidence_bundle(store, rest + sorted(external, reverse=True), fid)

    def test_budget_resume_restores_legacy_request_and_does_not_repay_completed_phases(self):
        data, calls, clock = self.data_with_two_sources(), [], [NOW]
        transport = self.provider(critique.CritiqueModel(), calls)
        with patch('rhino_invest.research._operation_evidence_bundle', side_effect=self.old_order):
            self.ingest(self.gateway(transport, daily=4, clock=lambda: clock[0]), data)
            result = self.ingest(self.gateway(transport, daily=4, clock=lambda: clock[0]), data)
        self.assertEqual(result['errors'][0]['error'], 'ModelBudgetExceeded')
        old = copy.deepcopy(self.store.items('research_repair'))
        clock[0] = '2026-10-24T15:00:00+00:00'
        model = self.gateway(transport, clock=lambda: clock[0])
        resumed = self.ingest(model, data, clock[0])
        self.assertEqual(resumed['errors'], [])
        self.assertEqual(resumed['plans'][0]['status'], 'active')
        self.assertEqual(self.store.items('research_repair'), old)
        self.ingest(model, data, clock[0])
        self.assertEqual(len(calls), 6)

    def test_legacy_unknown_call_stays_fenced_after_order_reconciliation(self):
        data, calls = self.data_with_two_sources(), []
        def failure(purpose, payload, body):
            if purpose == 'compile_plan' and payload.get('critique'):
                raise TimeoutError()
        transport = self.provider(critique.CritiqueModel(), calls, failure=failure)
        model = self.gateway(transport)
        with patch('rhino_invest.research._operation_evidence_bundle', side_effect=self.old_order):
            self.ingest(model, data)
            first = self.ingest(model, data)
        self.assertEqual(first['errors'][0]['error'], 'UnknownModelOutcome')
        for _ in range(2):
            self.assertEqual(self.ingest(model, data)['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(len(calls), 5)

    def test_cached_compile_survives_reversed_level_order_without_repeat_charge(self):
        class TwoLevels(critique.CritiqueModel):
            def run(self, purpose, payload, **kwargs):
                result = super().run(purpose, payload, **kwargs)
                if purpose == 'extract_levels':
                    result['levels'].append(dict(result['levels'][0], low='80', high='85'))
                if purpose == 'verify_levels':
                    result['checks'].append({'index': 1, 'verified': True, 'issues': []})
                return result
        data, calls = self.data_with_two_sources(), []
        transport = self.provider(TwoLevels(), calls)
        first = self.ingest(self.gateway(transport, daily=3), data)
        self.assertEqual(first['errors'][0]['error'], 'ModelBudgetExceeded')
        levels = [value for _, value in self.store.items('research_level')]
        self.assertEqual(len(levels), 2)
        snapshot = self.store.items('research_evidence')[0][1]
        plans = compile_plan(self.store, snapshot, list(reversed(levels)),
            self.gateway(transport, daily=4), {'NVDA': data}, NOW)
        self.assertEqual(len(plans), 1)
        self.assertEqual([purpose for purpose, _ in calls],
            ['extract_levels', 'verify_levels', 'compile_plan', 'review_plan'])

    def test_review_checkpoint_keeps_original_revision_assignment_before_reconstruction(self):
        data, calls, clock = self.data_with_two_sources(), [], [NOW]
        transport = self.provider(critique.CritiqueModel(), calls)
        self.ingest(self.gateway(transport, daily=5, clock=lambda: clock[0]), data)
        deferred = self.ingest(self.gateway(transport, daily=5, clock=lambda: clock[0]), data)
        self.assertEqual(deferred['errors'][0]['error'], 'ModelBudgetExceeded')
        checkpoint = copy.deepcopy(self.store.items('research_critique_review_input'))
        assignments = copy.deepcopy(self.store.items('plan_revision_assignment'))
        clock[0] = '2026-10-24T15:00:00+00:00'
        # An unkeyed observation on a later tick must not replace the already frozen review.
        snapshot = self.store.items('research_evidence')[0][1]
        levels = [value for _, value in self.store.items('research_level')]
        result = compile_plan(self.store, snapshot, levels,
            self.gateway(transport, clock=lambda: clock[0]), {'NVDA': data}, clock[0],
            context={'observation': 'later tick; no new review event'})
        self.assertEqual(result[0]['status'], 'active')
        self.assertEqual(self.store.items('research_critique_review_input'), checkpoint)
        self.assertEqual(self.store.items('plan_revision_assignment'), assignments)
        self.assertEqual(len(calls), 6)


if __name__ == '__main__':
    unittest.main()
