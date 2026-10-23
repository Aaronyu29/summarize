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
from rhino_invest.research import ingest_archives, review_plan, register_external_evidence, bind_instrument_identity, _run, COMPILE
from tests_invest.test_decisions import active_plan, market, portfolio
from rhino_invest.decisions import evaluate
from rhino_invest.config import Config
from rhino_invest.common import stable_id

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

    def run(self, purpose, payload, frames=(), now=None, instruction=None, call_scope=None):
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
            plan['entry'].update(basis='source_level', source_level_ids=[payload['levels'][0]['id']] if payload['levels'] else [],
                conditions=[payload['levels'][0]['conditions']] if payload['levels'] else [])
            plan['entry']['amount_usd'] = payload['policy']['amount_usd']
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
    def test_compile_prompt_embeds_valid_nested_json_template_without_example_prices(self):
        template = COMPILE.split('JSON_TEMPLATE_BEGIN\n', 1)[1].split('\nJSON_TEMPLATE_END', 1)[0]
        plan = json.loads(template)['plan']
        self.assertEqual([t['scenario'] for t in plan['targets']], ['base', 'bear'])
        self.assertTrue(all(t['price'] is None and t['assumptions']['eps'] is None for t in plan['targets']))
        self.assertEqual(plan['stop'], {'price': None, 'confirmation': 'daily_close'})
        self.assertFalse(plan['conditions_parsed'])

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
            'segments': [{'id': 35, 'start': 99, 'end': 105, 'text': 'Conditional creator entry, not execution'},
                         {'id': 36, 'start': 180, 'end': 185, 'text': 'Full original transcript remains available, including unrelated context.'}]})
        write(directory / 'visual-000.response.json', {'choices': [{'message': {
            'reasoning_content': 'PRIVATE_PROVIDER_REASONING_SENTINEL' * 100,
            'content': '{"visible_facts":"OLD_DERIVED_VISUAL_FACT_SENTINEL"}'}}], 'usage': {'total_tokens': 99999}})
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

    def add_revision(self, revision='2'):
        analysis = json.loads((self.root / 'analysis-v1.json').read_text(encoding='utf-8'))
        analysis['revision'] = revision
        sealed = self.root / f'analysis-v{revision}.json'
        write(sealed, analysis)
        manifest = json.loads((self.root / 'manifest-v1.json').read_text(encoding='utf-8'))
        manifest['analysis_sha256'] = digest(sealed)
        manifest['artifacts'] = [item for item in manifest['artifacts'] if item['path'] != 'analysis-v1.json']
        manifest['artifacts'].append({'path': sealed.name, 'sha256': digest(sealed)})
        write(self.root / f'manifest-v{revision}.json', manifest)
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?)', (VID, revision, str(self.root),
                'Latest sealed revision', json.dumps(analysis['videos'][0]), digest(sealed)))
        return sealed

    def test_all_revisions_archived_but_only_latest_researched(self):
        self.add_revision()
        model = FixtureModel()
        result = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(self.store.items('research_evidence')), 2)
        self.assertEqual([row['revision'] for row in result['historical_revisions']], ['1'])
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(len(result['plans']), 1)
        snapshot = self.store.get('research_evidence', result['plans'][0]['research_snapshot_id'])
        self.assertEqual(snapshot['revision'], '2')

    def test_corrupt_latest_does_not_fall_back_and_blocks_prior_advice(self):
        model = FixtureModel()
        plan = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)['plans'][0]
        self.store.put('advice_intent', 'pending', {'plan_id': plan['id'], 'status': 'pending'})
        self.store.put('reservation', 'cash', {'plan_id': plan['id'], 'status': 'active'})
        sealed = self.add_revision()
        sealed.write_bytes(b'corrupt-latest-seal')
        result = ingest_archives(self.store, self.db, model=model, now=NOW)
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(result['plans'], [])
        self.assertEqual(result['errors'][0]['revision'], '2')
        self.assertEqual(self.store.get('plan', plan['id'])['status'], 'needs_review')
        self.assertEqual(self.store.get('advice_intent', 'pending')['status'], 'cancelled')
        self.assertEqual(self.store.get('reservation', 'cash')['status'], 'released')

    def test_older_revision_is_still_verified(self):
        self.add_revision()
        (self.root / 'analysis-v1.json').write_bytes(b'corrupt-historical-seal')
        result = ingest_archives(self.store, self.db, now=NOW)
        self.assertEqual(result['errors'][0]['revision'], '1')
        self.assertEqual(result['ingested'], 1)

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
        changed_model = ModelGateway(self.store, dict(model.config, configuration_revision=2), transport)
        third = ingest_archives(self.store, self.db, model=changed_model, symbols=['NVDA', 'META'],
            fundamentals=dict(data, META={'financial_report': {'period': '2027'}}), now='2026-10-24T15:00:00+00:00')
        self.assertEqual(third['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(calls.count('compile_plan'), 1)

    def test_plan_cache_ignores_unrelated_watch_symbols_facts_and_identities(self):
        model = FixtureModel()
        data = {'NVDA': {'financial_report': {'period': '2025'}}}
        first = ingest_archives(self.store, self.db, model=model, symbols=['NVDA'], fundamentals=data, now=NOW)
        for symbols, facts in [(['NVDA', 'META'], data),
                               (['NVDA', 'META'], dict(data, META={'financial_report': {'period': '2026'}}))]:
            result = ingest_archives(self.store, self.db, model=model, symbols=symbols, fundamentals=facts,
                now='2026-10-24T15:00:00+00:00')
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['plans'], first['plans'])
        self.store.put('instrument_identity', 'META', {'id': 'unrelated-reviewed-identity'})
        result = ingest_archives(self.store, self.db, model=model, symbols=['NVDA', 'META'], fundamentals=data, now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual(model.calls, ['extract_levels', 'verify_levels', 'compile_plan', 'review_plan'])
        self.assertEqual(len(self.store.items('plan_revision')), 1)

    def test_plan_cache_recompiles_relevant_facts_identity_model_and_explicit_events(self):
        model = FixtureModel()
        model.config = {'configuration_revision': 1}
        data = {'NVDA': {'financial_report': {'period': '2025'}}}
        ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        data['NVDA']['financial_report']['period'] = '2026'
        ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        self.store.put('instrument_identity', 'NVDA', {'id': 'reviewed-identity',
            'metadata': {'is_adr': False, 'currency': 'USD', 'price_unit': 'per_share'}})
        ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        model.config['configuration_revision'] = 2
        result = ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        self.assertEqual(result['errors'], [])
        self.assertEqual(model.calls.count('compile_plan'), 4)
        self.assertEqual(model.calls.count('extract_levels'), 2)
        plan = result['plans'][0]
        for event in ['new-risk-one', 'new-risk-two']:
            result = review_plan(self.store, plan, model, event_id=event,
                evidence={'risk': 'Same facts, separately authorized new event'}, now=NOW)
            self.assertIsNotNone(result['revised_plan'])
        self.assertEqual(model.calls.count('compile_plan'), 6)
        self.assertEqual(model.calls.count('event_review'), 2)

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
            def run(self, purpose, payload, frames=(), now=None, instruction=None, call_scope=None):
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

    def test_model_transport_uses_primary_evidence_without_archived_provider_reasoning(self):
        fixture, requests = FixtureModel(), []
        def transport(body):
            requests.append(body)
            message = json.loads(body['messages'][1]['content'][0]['text'])
            result = fixture.run(message['purpose'], message['evidence'])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        model = ModelGateway(self.store, {'enabled': True, 'model': 'deepseek-flash', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '0.5', 'max_cost_usd_per_day': '4',
            'thinking': 'disabled', 'max_tokens': 12000, 'configuration_revision': 2}, transport)
        result = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025', 'currency': 'USD'}}}, now=NOW)
        self.assertEqual(result['errors'], [])
        snapshot = self.store.items('research_evidence')[0][1]
        self.assertIn('PRIVATE_PROVIDER_REASONING_SENTINEL', json.dumps(snapshot['visual_caches']))
        sent = json.dumps(requests)
        self.assertNotIn('PRIVATE_PROVIDER_REASONING_SENTINEL', sent)
        self.assertNotIn('OLD_DERIVED_VISUAL_FACT_SENTINEL', sent)
        self.assertNotIn('visual_caches', sent)
        payloads = {json.loads(body['messages'][1]['content'][0]['text'])['purpose']:
            json.loads(body['messages'][1]['content'][0]['text'])['evidence'] for body in requests}
        for purpose in ('extract_levels', 'verify_levels'):
            source = payloads[purpose]['source']
            self.assertEqual(source['transcript']['segments'], snapshot['transcript']['segments'])
            self.assertEqual(source['frames'][0]['sha256'], snapshot['frames'][0]['sha256'])
            self.assertEqual(source['evidence_refs'], [snapshot['id']])
            self.assertEqual(source['analysis_sha256'], snapshot['analysis_sha256'])
        for purpose in ('compile_plan', 'review_plan'):
            source = payloads[purpose]['source']
            self.assertEqual([v['id'] for v in source['transcript']['segments']], [35])
            self.assertEqual(source['transcript']['full_segment_count'], 2)
            self.assertIn('cited', source['transcript']['selection'])
        image_parts = [part for body in requests for part in body['messages'][1]['content'] if part['type'] == 'image_url']
        self.assertEqual(len(image_parts), 4)
        self.assertTrue(all(part['image_url']['detail'] == 'original' for part in image_parts))

    def test_complete_plan_uses_policy_amount_dates_and_real_review_frames(self):
        class PolicyModel(FixtureModel):
            def run(self, purpose, payload, **kwargs):
                if purpose in {'compile_plan', 'review_plan'}:
                    self.assert_policy = payload['policy']
                    assert len(kwargs.get('frames', [])) == 1
                    assert payload['policy']['cash_verified'] is False
                    assert payload['policy']['next_review_at'] == '2027-01-21T15:00:00+00:00'
                result = super().run(purpose, payload, **kwargs)
                if purpose == 'compile_plan':
                    result['plan']['entry']['amount_usd'] = None
                return result
        model = PolicyModel()
        result = ingest_archives(self.store, self.db, model=model,
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
        self.assertEqual(result['errors'], [])
        plan = result['plans'][0]
        self.assertEqual(plan['status'], 'active')
        self.assertEqual(plan['entry']['amount_usd'], '1000')
        self.assertEqual(plan['thesis_valid_from'], NOW)
        self.assertEqual(plan['horizon_until'], '2028-10-23T15:00:00+00:00')
        self.assertEqual(model.frame_counts, [1, 1, 1, 1])

    def test_etf_missing_nav_is_typed_research_gap_without_paid_eps_plan(self):
        class ETFModel(FixtureModel):
            def run(self, purpose, payload, **kwargs):
                assert purpose in {'extract_levels', 'verify_levels'}, 'ETF company-EPS route must not be called'
                result = super().run(purpose, payload, **kwargs)
                if purpose == 'extract_levels':
                    result['levels'][0]['symbol'] = 'SOXX'
                return result
        result = ingest_archives(self.store, self.db, model=ETFModel(), fundamentals={'SOXX': {
            'identity': [{'symbol': 'SOXX.US', 'currency': 'USD', 'name_en': 'iShares Semiconductor ETF'}],
            'financial_report': {'list': {'IS': {'fund_eps': '-31.118'}}}}}, now=NOW)
        self.assertEqual(result['errors'], [])
        plan = result['plans'][0]
        self.assertEqual(plan['status'], 'needs_review')
        self.assertEqual(plan['research_gaps'][0]['code'], 'ETF_VALUATION_EVIDENCE_UNAVAILABLE')
        self.assertEqual(plan['targets'], [])
        self.assertEqual(plan['entry']['amount_usd'], '1000')
        self.assertNotIn('horizon_until', plan)

    def test_spliced_creator_entry_and_technical_support_never_become_buy_on_assurance(self):
        for technical in (False, True):
            with Store(':memory:') as store:
                class BadEntryModel(FixtureModel):
                    def run(self, purpose, payload, **kwargs):
                        result = super().run(purpose, payload, **kwargs)
                        if purpose == 'extract_levels' and technical:
                            result['levels'][0]['kind'] = 'technical_support'
                        if purpose == 'compile_plan' and not technical:
                            result['plan']['entry']['high'] = '120'
                        return result
                result = ingest_archives(store, self.db, model=BadEntryModel(),
                    fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['plans'][0]['status'], 'needs_review')
                self.assertNotIn('thesis_valid_from', result['plans'][0])

    def test_event_risk_is_independently_sealed_before_plan_invalidation_without_recompile(self):
        from rhino_invest.decisions import valid_thesis_events
        plan = ingest_archives(self.store, self.db, model=FixtureModel(),
            fundamentals={'NVDA': {'financial_report': {'period': '2025'}}}, now=NOW)['plans'][0]
        self.store.put('advice_intent', 'pending', {'plan_id': plan['id'], 'status': 'pending'})
        self.store.put('reservation', 'cash', {'plan_id': plan['id'], 'status': 'active'})
        class RiskModel:
            def __init__(self):
                self.calls = []
            def run(self, purpose, payload, **kwargs):
                self.calls.append(purpose)
                if purpose == 'event_review':
                    return {'assessment': {'triggered': True, 'condition': 'cashflow_failed',
                        'evidence_refs': payload['new_evidence_refs'], 'rationale': 'New original report documents condition'}}
                assert purpose == 'verify_invalidation'
                return {'accepted': True, 'condition': 'cashflow_failed',
                    'evidence_refs': payload['assessment']['evidence_refs'], 'issues': []}
        model = RiskModel()
        result = review_plan(self.store, plan, model, event_id='new-cashflow-report',
            evidence={'source': 'offline-primary-fixture', 'disclosed_cashflow': '-100'}, now=NOW)
        self.assertEqual(model.calls, ['event_review', 'verify_invalidation'])
        self.assertEqual(result['status'], 'invalidated')
        self.assertIsNone(result['revised_plan'])
        current = self.store.get('plan', plan['id'])
        self.assertEqual(current['status'], 'invalidated')
        self.assertEqual(len(valid_thesis_events(self.store, current, NOW)), 1)
        self.assertEqual(len(self.store.items('plan_revision')), 1)
        self.assertEqual(self.store.get('advice_intent', 'pending')['status'], 'cancelled')
        self.assertEqual(self.store.get('reservation', 'cash')['status'], 'released')

    def test_unresolved_sealed_risk_blocks_cached_new_financial_and_new_source_plans(self):
        from rhino_invest.decisions import record_thesis_risk, valid_thesis_events
        data = {'NVDA': {'financial_report': {'period': '2025'}}}
        plan = ingest_archives(self.store, self.db, model=FixtureModel(), fundamentals=data, now=NOW)['plans'][0]
        self.store.put('external_evidence', 'raw-risk-report', {'source': 'offline original report', 'disclosure': 'condition occurred'}, immutable=True)
        risk = record_thesis_risk(self.store, plan,
            {'triggered': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-risk-report'], 'rationale': 'Disclosed condition'},
            {'accepted': True, 'condition': 'cashflow_failed', 'evidence_refs': ['raw-risk-report'], 'issues': []}, now=NOW)
        original = dict(plan, status='invalidated')
        self.store.put('plan', plan['id'], original)
        model = FixtureModel()
        # A completed old workflow must not return its cached active plan copy.
        cached = ingest_archives(self.store, self.db, model=model, fundamentals=data, now=NOW)
        self.assertEqual(cached['plans'], [])
        self.assertEqual(cached['deferred_unresolved_risk'][0]['risk_evidence_ids'], [risk['id']])
        # A frozen attempt clock cannot hide an already sealed unresolved risk.
        changed = {'NVDA': {'financial_report': {'period': '2026'}}}
        for moment in ['2026-10-23T14:00:00+00:00', '2026-10-24T15:00:00+00:00']:
            result = ingest_archives(self.store, self.db, model=model, fundamentals=changed, now=moment)
            self.assertEqual(result['plans'], [])
            self.assertEqual(result['deferred_unresolved_risk'][0]['code'], 'UNRESOLVED_VERIFIED_THESIS_RISK')
        self.assertEqual(model.calls, [])
        self.add_revision()
        result = ingest_archives(self.store, self.db, model=model, fundamentals=changed, now=NOW)
        self.assertEqual(result['plans'], [])
        self.assertEqual(model.calls, ['extract_levels', 'verify_levels'])
        self.assertEqual(self.store.get('plan', plan['id']), original)
        self.assertEqual(len(self.store.items('plan_revision')), 1)
        self.assertEqual(len(valid_thesis_events(self.store, original, NOW)), 1)
        defer = result['deferred_unresolved_risk'][0]
        self.assertIn('raw-risk-report', defer['original_evidence_hashes'])
        review = review_plan(self.store, original, model, event_id='new-facts-after-risk', evidence=changed, now=NOW)
        self.assertEqual(review['status'], 'deferred')
        self.assertEqual(model.calls, ['extract_levels', 'verify_levels'])

    def test_compile_unknown_scope_does_not_change_with_derived_level_ids(self):
        ingest_archives(self.store, self.db, now=NOW)
        snapshot = self.store.items('research_evidence')[0][1]
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        config = {'enabled': True, 'model': 'deepseek-flash', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '0.5', 'max_cost_usd_per_day': '4'}
        payload = {'symbol': 'NVDA', 'source': snapshot, 'levels': [],
            'fundamentals': {'financial_report': {'period': '2025'}}, 'evidence_ids': ['source', 'old-derived-level']}
        with self.assertRaises(UnknownModelOutcome):
            _run(ModelGateway(self.store, config, transport), 'compile_plan', payload, now=NOW)
        payload['evidence_ids'] = ['source', 'new-derived-level']
        with self.assertRaises(UnknownModelOutcome):
            _run(ModelGateway(self.store, dict(config, configuration_revision=2), transport),
                'compile_plan', payload, now='2026-10-24T15:00:00+00:00')
        self.assertEqual(len(calls), 1)

    def test_legacy_unknown_without_images_blocks_new_image_and_instrument_contract(self):
        ingest_archives(self.store, self.db, now=NOW)
        snapshot = self.store.items('research_evidence')[0][1]
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        config = {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '0.5', 'max_cost_usd_per_day': '4'}
        payload = {'symbol': 'NVDA', 'source': snapshot, 'levels': [],
            'fundamentals': {'financial_report': {'period': '2025'}}}
        with self.assertRaises(UnknownModelOutcome):
            _run(ModelGateway(self.store, config, transport), 'compile_plan', payload, now=NOW)
        payload['fundamentals']['instrument_contract'] = {'asset_type': 'equity', 'quote_price_unit': 'per_share'}
        frame = (100, self.root / VID / 'frames/frame-00100.jpg')
        with self.assertRaises(UnknownModelOutcome):
            _run(ModelGateway(self.store, dict(config, configuration_revision=2), transport),
                'compile_plan', payload, frames=[frame], now='2026-10-24T15:00:00+00:00', store=self.store)
        self.assertEqual(len(calls), 1)

    def test_plan_frame_limit_is_explicit_and_never_truncates_references(self):
        from rhino_invest.research import _plan_frames
        frames = []
        for second in range(100, 109):
            path = self.root / VID / f'frames/frame-{second:05}.jpg'
            path.write_bytes(str(second).encode())
            frames.append({'timestamp_seconds': second, 'path': f'frames/{path.name}', 'sha256': digest(path)})
        snapshot = {'source': str(self.root), 'video_id': VID, 'frames': frames}
        levels = [{'verified': True, 'frame_seconds': second} for second in range(100, 109)]
        with self.assertRaisesRegex(ValueError, 'more than eight'):
            _plan_frames(snapshot, levels)

    def test_legacy_live_empty_consensus_flag_cannot_unlock_unknown_after_contract_upgrade(self):
        ingest_archives(self.store, self.db, now=NOW)
        snapshot = self.store.items('research_evidence')[0][1]
        old_fund = {'financial_report': {'period': '2025'}, 'consensus': {'currency': '', 'list': []},
            'consensus_live_verified': True, 'consensus_normalized': {'forecasts': []}}
        old_scope = stable_id('research-logical-call', 'compile_plan', snapshot['id'], 'NVDA', old_fund, None, [])
        calls = []
        def transport(body):
            calls.append(body)
            raise TimeoutError()
        config = {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '0.5', 'max_cost_usd_per_day': '4'}
        model = ModelGateway(self.store, config, transport)
        with self.assertRaises(UnknownModelOutcome):
            model.run('compile_plan', {'old': 'payload'}, call_scope=old_scope)
        new_fund = dict(old_fund, consensus_live_verified=False, consensus_response_received=True,
            consensus_normalized={'forecasts': [], 'coverage': {'future_eps_quarters': 0}})
        payload = {'symbol': 'NVDA', 'source': snapshot, 'levels': [], 'fundamentals': new_fund}
        with self.assertRaises(UnknownModelOutcome):
            _run(model, 'compile_plan', payload, frames=[(100, self.root / VID / 'frames/frame-00100.jpg')], store=self.store)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
