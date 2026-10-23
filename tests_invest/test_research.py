import hashlib
import contextlib
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest

from rhino_invest.store import Store
from rhino_invest.model import ModelGateway, UnknownModelOutcome
from rhino_invest.research import ingest_archives, review_plan, register_external_evidence, bind_instrument_identity
from tests_invest.test_decisions import active_plan, market, portfolio
from rhino_invest.decisions import evaluate
from rhino_invest.config import Config

NOW = '2026-10-23T15:00:00+00:00'
VID = '4_uytm7I_XQ'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FixtureModel:
    def __init__(self, uncertain=False, adr=False):
        self.calls, self.uncertain, self.adr, self.frame_counts = [], uncertain, adr, []

    def run(self, purpose, payload, frames=(), now=None, instruction=None):
        self.calls.append(purpose)
        self.frame_counts.append(len(frames))
        symbol = 'TSM' if self.adr else 'NVDA'
        if purpose == 'extract_levels':
            return {'levels': [{'symbol': symbol, 'kind': 'creator_entry', 'low': '90', 'high': '110',
                'value': None, 'currency': 'USD', 'price_unit': 'per_adr' if self.adr else 'per_share',
                'source_date': '2026-10-22', 'adjustment': 'NoAdjust', 'source_horizon': 'creator conditional entry',
                'conditions': None if self.uncertain else 'Conditional future entry; not executed',
                'evidence_segment_ids': [35], 'frame_seconds': 100, 'timestamp_seconds': 100,
                'uncertainty': 'No long term target in source'}]}
        if purpose == 'verify_levels':
            return {'checks': [{'index': 0, 'verified': True, 'issues': []}]}
        if purpose == 'compile_plan':
            plan = active_plan(symbol)
            plan['evidence_refs'] = payload['evidence_ids']
            plan['is_adr'] = self.adr
            plan['price_unit'] = 'per_adr' if self.adr else 'per_share'
            plan['targets'] = []
            for scenario, eps in [('base', '5'), ('bear', '3')]:
                plan['targets'].append({'scenario': scenario, 'price': str(int(eps) * 30), 'horizon_months': 24,
                    'method': 'eps_multiple', 'assumptions': {'eps': eps, 'multiple': '30', 'currency': 'USD',
                        'price_unit': plan['price_unit'], 'forecast_period': '2028 FY',
                        'rationale': 'Independent scenario, not reported consensus', 'evidence_refs': payload['evidence_ids']},
                    'evidence_refs': payload['evidence_ids']})
            return {'plan': plan}
        return {'accepted': True, 'issues': [], 'checks': {'provenance': True, 'currency': True, 'unit': True, 'adr': True}}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / 'research-data/test-tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        directory = self.root / VID
        directory.mkdir()
        (directory / 'media.f139.m4a').write_bytes(b'original-audio')
        frame = directory / 'frames/frame-00100.jpg'
        frame.parent.mkdir()
        frame.write_bytes(b'original-frame')
        write(directory / 'frames.json', [{'timestamp_seconds': 100, 'path': 'frames\\frame-00100.jpg', 'sha256': digest(frame)}])
        write(directory / f'{VID}.info.json', {'id': VID, 'duration': 200, 'upload_date': '20261022'})
        write(directory / 'transcript.asr.json', {'audio_sha256': digest(directory / 'media.f139.m4a'),
            'segments': [{'id': 35, 'start': 99, 'end': 105, 'text': 'Conditional creator entry, not execution'}]})
        write(directory / 'visual-000.response.json', {'visual_facts': 'USD per share'})
        analysis = {'video_id': VID, 'claims': [{'evidence_segment_ids': [35], 'frame_seconds': 100}]}
        sealed = self.root / 'analysis-v1.json'
        write(sealed, {'videos': [analysis]})
        manifest = {'analysis_sha256': digest(sealed), 'artifacts': [
            {'path': str(p.relative_to(self.root)).replace('\\', '/'), 'sha256': digest(p)}
            for p in self.root.rglob('*') if p.is_file()]}
        write(self.root / 'manifest-v1.json', manifest)
        self.db = self.root / 'library.sqlite'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE analyses(video_id,revision,source,title,payload,analysis_sha256)')
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?)', (VID, '1', str(self.root), 'SOXX/TSM archived sample',
                json.dumps(analysis), digest(sealed)))

    def test_seal_read_only_model_workflow_cache_and_integer_refs(self):
        model = FixtureModel()
        before = self.db.read_bytes()
        data = {'NVDA': {'financial_report': {'currency': 'USD', 'period': '2025', 'income_statement': {}}, 'fetched_at': NOW}}
        result = ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['plans'][0]['status'], 'active')
        level = self.store.items('research_level')[0][1]
        self.assertTrue(level['verified'])
        self.assertEqual(level['evidence_segment_ids'], [35])
        self.assertEqual(self.db.read_bytes(), before)
        data['NVDA']['fetched_at'] = '2026-10-24T15:00:00+00:00'
        again = ingest_archives(self.store, self.db, model=model, fundamentals=data, now='2026-10-24T15:00:00+00:00')
        self.assertEqual(again['unchanged'], 1)
        self.assertEqual(len(model.calls), 4)

    def test_uncertain_source_and_unverified_adr_cannot_activate(self):
        for uncertain, adr in [(True, False), (False, True)]:
            with Store(':memory:') as store:
                model = FixtureModel(uncertain=uncertain, adr=adr)
                result = ingest_archives(store, self.db, model=model,
                    fundamentals={'TSM' if adr else 'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['plans'][0]['status'], 'needs_review')

    def test_mutated_frame_rejected_even_after_prior_ingest(self):
        result = ingest_archives(self.store, self.db, now=NOW)
        self.assertEqual(result['ingested'], 1)
        (self.root / VID / 'frames/frame-00100.jpg').write_bytes(b'changed')
        result = ingest_archives(self.store, self.db, model=FixtureModel(), now=NOW)
        self.assertEqual(result['ingested'], 0)
        self.assertIn('fingerprint mismatch', result['errors'][0]['detail'])

    def test_explicit_event_review_is_once_per_evidence_event(self):
        model = FixtureModel()
        a = review_plan(self.store, active_plan(), model, event_id='2026-Q4', now=NOW)
        b = review_plan(self.store, active_plan(), model, event_id='2026-Q4', now=NOW)
        self.assertEqual(a, b)
        self.assertEqual(model.calls, ['event_review'])

    def test_review_produces_new_sealed_revision_and_cancels_commitment(self):
        model = FixtureModel()
        plan = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}, 'fetched_at': NOW}}, now=NOW)['plans'][0]
        self.store.put('advice_intent', 'pending', {'plan_id': plan['id'], 'status': 'pending'})
        self.store.put('reservation', 'cash', {'plan_id': plan['id'], 'status': 'active'})
        record = review_plan(self.store, plan, model, event_id='new-risk-evidence',
            evidence={'risk': 'New financing disclosure'}, now='2026-10-24T15:00:00+00:00')
        updated = record['revised_plan']
        self.assertEqual(updated['revision'], 2)
        self.assertGreater(updated['next_review_at'], plan['next_review_at'])
        self.assertEqual(len(self.store.items('plan_revision')), 2)
        self.assertEqual(self.store.get('advice_intent', 'pending')['status'], 'cancelled')
        self.assertEqual(self.store.get('reservation', 'cash')['status'], 'released')
        before = len(model.calls)
        review_plan(self.store, plan, model, event_id='new-risk-evidence',
            evidence={'risk': 'New financing disclosure'}, now='2026-10-24T16:00:00+00:00')
        self.assertEqual(len(model.calls), before)
        fundamental = self.store.get('fundamental_evidence', updated['fundamental_evidence_id'])
        self.assertEqual(fundamental['data']['fetched_at'], NOW)

    def test_reviewed_primary_identity_binding_unblocks_adr_without_guessing(self):
        source = self.root / 'reviewed-identity.txt'
        source.write_text('Offline fixture primary disclosure: 1 ADR represents 5 ordinary shares.', encoding='utf-8')
        evidence = register_external_evidence(self.store, source, source_url='https://issuer.invalid/adr',
            as_of=NOW, reviewed_by='offline reviewer')
        bind_instrument_identity(self.store, 'TSM', {'is_adr': True, 'ordinary_shares_per_adr': '5',
            'underlying_currency': 'TWD', 'currency': 'USD', 'price_unit': 'per_adr'},
            evidence_refs=[evidence], reviewed_by='offline reviewer', reviewed_at=NOW)
        result = ingest_archives(self.store, self.db, model=FixtureModel(adr=True),
            fundamentals={'TSM': {'financial_report': {'period': '2025'}}}, now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['plans'][0]['status'], 'active')
        self.assertTrue(result['plans'][0]['verification']['adr'])

    def test_image_batches_and_fundamental_refresh_reuse_source_extraction(self):
        directory = self.root / VID
        frames = []
        for second in range(100, 117):
            path = directory / f'frames/frame-{second:05}.jpg'
            path.write_bytes(('frame' + str(second)).encode())
            frames.append({'timestamp_seconds': second, 'path': f'frames/{path.name}', 'sha256': digest(path)})
        write(directory / 'frames.json', frames)
        write(self.root / 'manifest-v1.json', {'analysis_sha256': digest(self.root / 'analysis-v1.json'),
            'artifacts': [{'path': str(p.relative_to(self.root)).replace('\\', '/'), 'sha256': digest(p)}
                for p in self.root.rglob('*') if p.is_file() and p.name != 'manifest-v1.json']})
        model = FixtureModel()
        first = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
        self.assertEqual(first['errors'], [])
        self.assertEqual(model.calls.count('extract_levels'), 3)
        self.assertLessEqual(max(model.frame_counts), 8)
        refreshed = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2026'}}}, now=NOW)
        self.assertEqual(refreshed['errors'], [])
        self.assertEqual(model.calls.count('extract_levels'), 3)
        self.assertEqual(refreshed['plans'][0]['revision'], 2)

    def test_unknown_compile_retry_reuses_original_clock_and_call_fence(self):
        fixture, calls = FixtureModel(), []
        def transport(body):
            payload = json.loads(body['messages'][1]['content'][0]['text'])
            purpose = payload['purpose']
            calls.append(purpose)
            if purpose == 'compile_plan':
                raise TimeoutError('ambiguous provider outcome')
            output = fixture.run(purpose, payload['evidence'])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}
        model = ModelGateway(self.store, {'enabled': True, 'model': 'fixture', 'base_url': 'https://offline.invalid/v1',
            'max_calls_per_day': 20, 'max_cost_usd_per_call': '1', 'max_cost_usd_per_day': '20'}, transport)
        data = {'NVDA': {'financial_report': {'period': '2025'}}}
        first = ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        second = ingest_archives(self.store, self.db, model=model, fundamentals=data, now='2026-10-23T15:01:00+00:00')
        self.assertEqual(first['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(second['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(calls.count('compile_plan'), 1)
        self.assertEqual(len(self.store.items('model_call')), 3)

    def test_raw_financial_change_and_latest_quarterly_cache_feed_revisions(self):
        model = FixtureModel()
        plan = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025', 'income': '100'}}}, now=NOW)['plans'][0]
        changed = {'symbol': 'NVDA', 'financial_report': {'period': '2026', 'income': '150'}, 'fetched_at': NOW}
        record = review_plan(self.store, plan, model, event_id='report-change', evidence=changed, now=NOW)
        revised = record['revised_plan']
        evidence = self.store.get('fundamental_evidence', revised['fundamental_evidence_id'])
        self.assertEqual(evidence['data']['financial_report']['income'], '150')
        self.store.put('fundamental_cache', 'NVDA', {'data': dict(changed, financial_report={'period': '2027', 'income': '200'})})
        next_review = review_plan(self.store, revised, model, event_id='quarter', evidence={'period': '2027-Q1'}, now=NOW)
        evidence = self.store.get('fundamental_evidence', next_review['revised_plan']['fundamental_evidence_id'])
        self.assertEqual(evidence['data']['financial_report']['income'], '200')

    def test_older_lexically_last_source_never_overwrites_latest_thesis(self):
        old_id = 'ZZZZZZZZZZZ'
        older = self.root / 'older'
        shutil.copytree(self.root / VID, older / old_id)
        info = older / old_id / (VID + '.info.json')
        info.rename(older / old_id / (old_id + '.info.json'))
        write(older / old_id / (old_id + '.info.json'), {'id': old_id, 'duration': 200, 'upload_date': '20261001'})
        analysis = {'video_id': old_id, 'claims': [{'evidence_segment_ids': [35], 'frame_seconds': 100}]}
        write(older / 'analysis-v1.json', {'videos': [analysis]})
        write(older / 'manifest-v1.json', {'analysis_sha256': digest(older / 'analysis-v1.json'),
            'artifacts': [{'path': str(p.relative_to(older)).replace('\\', '/'), 'sha256': digest(p)}
                for p in older.rglob('*') if p.is_file() and p.name != 'manifest-v1.json']})
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?)', (old_id, '1', str(older), 'Older source', json.dumps(analysis), digest(older / 'analysis-v1.json')))
        model = FixtureModel()
        first = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
        self.assertEqual(first['errors'], [])
        current = self.store.items('plan')[0][1]
        self.assertEqual(current['source_published_at'], '2026-10-22T00:00:00+00:00')
        refreshed = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2026'}}}, now=NOW)
        self.assertEqual(refreshed['errors'], [])
        self.assertEqual(model.calls.count('compile_plan'), 3)
        self.assertEqual(self.store.items('plan')[0][1]['source_published_at'], '2026-10-22T00:00:00+00:00')

    def test_valid_plan_evidence_subset_binds_its_exact_reference_closure(self):
        class SubsetModel(FixtureModel):
            def run(self, purpose, payload, **kwargs):
                result = super().run(purpose, payload, **kwargs)
                if purpose == 'compile_plan':
                    result['plan']['evidence_refs'] = payload['evidence_ids'][:1]
                    for target in result['plan']['targets']:
                        target['evidence_refs'] = payload['evidence_ids'][:2]
                        target['assumptions']['evidence_refs'] = payload['evidence_ids'][:2]
                return result
        result = ingest_archives(self.store, self.db, model=SubsetModel(),
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
        plan = result['plans'][0]
        self.assertEqual(plan['status'], 'active')
        rec = evaluate(self.store, plan, market('NVDA'), portfolio('5000'), NOW)
        self.assertEqual(rec['action'], 'buy')

    def test_real_soxx_tsm_archive_replay_keeps_unsupported_targets_for_review(self):
        archive = Path(__file__).resolve().parents[1] / 'research-data/library.sqlite'
        if not archive.is_file():
            self.skipTest('Optional local sealed research archive is not distributed in CI')
        with contextlib.closing(sqlite3.connect(archive.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT video_id,revision,source,title,payload,analysis_sha256 FROM analyses WHERE video_id=?', (VID,)).fetchone()
        if not row:
            self.skipTest('Real SOXX/TSM sample unavailable')
        replay_db = self.root / 'real-replay.sqlite'
        with contextlib.closing(sqlite3.connect(replay_db)) as db, db:
            db.execute('CREATE TABLE analyses(video_id,revision,source,title,payload,analysis_sha256)')
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?)', row)
        class ReplayModel:
            def run(self, purpose, payload, frames=(), now=None, instruction=None):
                if purpose == 'extract_levels':
                    available = {second for second, _ in frames}
                    levels = []
                    if 1495 in available:
                        for low, high, fraction in [('360', '378', '2%'), ('320', '350', '4%')]:
                            levels.append({'symbol': 'TSM', 'kind': 'creator_entry', 'low': low, 'high': high,
                                'currency': 'USD', 'price_unit': 'per_adr', 'source_date': '2026-10-23',
                                'adjustment': None, 'source_horizon': None, 'conditions': 'Creator planned add ' + fraction + '; not an executed user trade',
                                'evidence_segment_ids': [603, 604], 'frame_seconds': 1495,
                                'timestamp_seconds': 1496, 'uncertainty': 'Oral broad range differs from two visual tranches'})
                    if 1765 in available:
                        for price in ['486', '536']:
                            levels.append({'symbol': 'SOXX', 'kind': 'technical_support', 'value': price,
                                'currency': 'USD', 'price_unit': 'per_share', 'source_date': '2026-10-23',
                                'adjustment': None, 'source_horizon': 'short-term technical retest',
                                'conditions': 'Previously broken support/retest resistance, not a long-term target',
                                'evidence_segment_ids': [713, 717], 'frame_seconds': 1765,
                                'timestamp_seconds': 1733, 'uncertainty': 'Not verified fundamental valuation'})
                    return {'levels': levels}
                if purpose == 'verify_levels':
                    return {'checks': [{'index': i, 'verified': False, 'issues': ['Adjustment and long-term target unsupported']}
                        for i, _ in enumerate(payload['levels']['levels'])]}
                if purpose == 'compile_plan':
                    plan = active_plan(payload['symbol'])
                    plan.update(evidence_refs=payload['evidence_ids'], targets=[], take_profit=[],
                        stop={'price': None, 'confirmation': 'daily_close'})
                    return {'plan': plan}
                return {'accepted': False, 'issues': ['Independent long-term targets lack verified inputs'], 'checks': {}}
        result = ingest_archives(self.store, replay_db, model=ReplayModel(), symbols=['TSM', 'SOXX'], now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual({p['symbol'] for p in result['plans']}, {'TSM', 'SOXX'})
        self.assertTrue(all(p['status'] == 'needs_review' and not p['targets'] for p in result['plans']))
        self.assertEqual(len(self.store.items('research_level')), 4)

    def test_research_since_keeps_old_seals_without_starting_paid_workflow(self):
        model = FixtureModel()
        result = ingest_archives(self.store, self.db, model=model, research_since='2026-10-23T02:10:36Z', now=NOW)
        self.assertEqual(result['ingested'], 1)
        self.assertEqual(len(self.store.items('research_evidence')), 1)
        self.assertEqual(len(result['deferred_before_since']), 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(model.calls, [])
        self.assertEqual(self.store.items('research_attempt'), [])
        self.assertEqual(self.store.items('research_workflow'), [])
        # The cutoff is inclusive; relaxing it safely processes the retained seal.
        included = ingest_archives(self.store, self.db, model=model,
            research_since='2026-10-22T00:00:00Z', now=NOW)
        self.assertEqual(included['unchanged'], 1)
        self.assertEqual(included['deferred_before_since'], [])
        self.assertEqual(model.calls, ['extract_levels', 'verify_levels', 'compile_plan', 'review_plan'])

    def test_research_since_config_environment_normalization_and_validation(self):
        config = Config.load(environ={'INVEST_RESEARCH_SINCE': '2026-10-23T10:10:36+08:00'})
        self.assertEqual(config.research_since, '2026-10-23T02:10:36+00:00')
        self.assertEqual(config.diagnostics()['research_since'], config.research_since)
        with self.assertRaises(ValueError):
            Config.load(environ={'INVEST_RESEARCH_SINCE': '2026-10-23T10:10:36'})
        with self.assertRaises(ValueError):
            ingest_archives(self.store, self.db, research_since='not-a-date')


if __name__ == '__main__':
    unittest.main()
