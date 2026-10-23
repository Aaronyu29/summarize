"""Read sealed video revisions and produce separately reviewed long-term plans."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from datetime import timedelta, datetime, timezone
from .common import utc_now, stable_id, validate_plan, decimal_value, parse_time
from .market import symbol_value


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(root, relative):
    path = (root / relative.replace('\\', '/')).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Evidence path escapes archive root')
    return path


def _snapshot(row, now):
    video_id, revision, source, title, payload, expected = row
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,32}', revision):
        raise ValueError('Invalid sealed revision')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid video identity')
    root = Path(source).resolve()
    sealed = root / f'analysis-v{revision}.json'
    if _sha(sealed) != expected:
        raise ValueError('Sealed analysis fingerprint mismatch')
    analysis = _read(sealed)
    videos = [v for v in analysis['videos'] if v['video_id'] == video_id]
    if len(videos) != 1 or videos[0] != json.loads(payload):
        raise ValueError('Database and sealed analysis disagree')
    manifest_path = root / f'manifest-v{revision}.json'
    manifest = _read(manifest_path)
    if manifest['analysis_sha256'] != expected:
        raise ValueError('Manifest does not seal analysis')
    artifacts = {v['path'].replace('\\', '/'): v['sha256'] for v in manifest['artifacts']}
    files = []
    def capture(relative, expected_hash=None):
        relative = relative.replace('\\', '/')
        path = _safe(root, relative)
        digest = _sha(path)
        if relative not in artifacts or digest != artifacts[relative] or (expected_hash and digest != expected_hash):
            raise ValueError('Evidence fingerprint mismatch: ' + relative)
        files.append({'path': str(path), 'relative_path': relative, 'sha256': digest})
        return path
    prefix = video_id + '/'
    info = _read(capture(prefix + video_id + '.info.json'))
    if info.get('id') != video_id:
        raise ValueError('Source metadata identity mismatch')
    transcript = _read(capture(prefix + 'transcript.asr.json'))
    frames = _read(capture(prefix + 'frames.json'))
    capture(prefix + 'media.f139.m4a', transcript.get('audio_sha256'))
    for frame in frames:
        capture(prefix + frame['path'], frame['sha256'])
    caches = {}
    for relative in sorted(artifacts):
        if relative.startswith(prefix + 'visual') and relative.endswith('.response.json'):
            caches[relative] = _read(capture(relative))
    snapshot = {'video_id': video_id, 'revision': revision, 'title': title,
        'source': str(root), 'analysis_sha256': expected, 'manifest_sha256': _sha(manifest_path),
        'analysis': videos[0], 'metadata': {'id': info['id'], 'upload_date': info.get('upload_date'),
            'timestamp': info.get('timestamp'), 'duration': info.get('duration')},
        'transcript': transcript, 'frames': frames, 'visual_caches': caches, 'files': files}
    snapshot['id'] = stable_id('research-evidence-v1', snapshot)
    snapshot['ingested_at'] = now
    return snapshot


def _published_at(metadata):
    stamp = metadata.get('timestamp')
    if isinstance(stamp, (float, int)) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    date = metadata.get('upload_date')
    if isinstance(date, str) and re.fullmatch(r'\d{8}', date):
        return datetime.strptime(date, '%Y%m%d').replace(tzinfo=timezone.utc).isoformat()
    raise ValueError('Verified publication date is required for current research ordering')


def _row_order(row):
    try:
        return _published_at(_read(Path(row[2]) / row[0] / (row[0] + '.info.json'))), row[0], row[1]
    except (ValueError, OSError, KeyError):
        return '', row[0], row[1]


EXTRACT = '''Extract individual price/valuation/stop/target levels only from the supplied
sealed transcript, original frames and visual caches. Return {"levels":[{"symbol":"TSM",
"kind":"creator_entry|technical_support|valuation|historical_quote|target|stop",
"value":null,"low":null,"high":null,"currency":null,"price_unit":null,
"source_date":null,"adjustment":null,"source_horizon":null,"timestamp_seconds":0,
"evidence_segment_ids":[],"frame_seconds":null,"conditions":null,"uncertainty":null}]}.
Keep conditional plans separate from executed trades. Never treat a historical quote or
technical resistance as an independent long-term price target. Retain ambiguous values.'''
VERIFY = '''Reverify every extracted individual level against original transcript/frames,
including units, currency, dates, adjustment, horizon and conditions. Return
{"checks":[{"index":0,"verified":false,"issues":[]}]} with exactly one check per level.
Missing or ambiguous evidence must be verified=false; never repair by guessing.'''
COMPILE = '''Build an INDEPENDENT 12-36 month investment plan from the verified evidence and
raw financial reports/valuation below. The creator's technical levels are context only.
Return {"plan":{symbol,thesis,horizon_months,next_review_at,currency,price_unit,is_adr,
entry:{low,high,amount_usd},targets:[{scenario,price,horizon_months,method,assumptions,
evidence_refs}],take_profit:[{price,fraction}],stop:{price,confirmation:"daily_close"},
invalidation_conditions:[],time_exit_policy,valuation_basis:{},conditions_parsed:true,
evidence_refs:[]}}. Use evidence_refs ONLY from supplied evidence IDs. Target method must
be eps_multiple with assumptions {eps,multiple,currency,price_unit,forecast_period,
rationale,evidence_refs}; target=eps*multiple, at least base/bear scenarios. Forecast EPS
and multiple are explicitly scenario assumptions, never pretend they are disclosed.
Leave unsupported fields null and state gaps; missing data means needs_review, not a buy.
Do not use illustrative user numbers 370/800/270. No generic automatic upside percentage.'''
REVIEW = '''Independently review each target scenario, arithmetic, explicit assumptions,
financial source periods/units, ADR conversion, downside, entry/stop/TP, thesis invalidation
and 12-36 month horizon. Return {"accepted":false,"issues":[],"checks":{
"provenance":false,"currency":false,"unit":false,"adr":false}}. Only accept if all
material fields have evidence; ambiguous currency/units/ADR forbid activation.'''


def _run(model, purpose, payload, frames=(), now=None):
    payload = dict(payload)
    instruction = payload.pop('instruction', None)
    return model.run(purpose, payload, frames=frames, now=now, instruction=instruction)


def _frames(snapshot):
    result = []
    for frame in snapshot['frames']:
        path = _safe(Path(snapshot['source']), snapshot['video_id'] + '/' + frame['path'])
        if _sha(path) != frame['sha256']:
            raise ValueError('Original frame changed after sealing')
        result.append((frame['timestamp_seconds'], path))
    return result


def _verify_levels(snapshot, extracted, checked):
    levels = extracted.get('levels', [])
    checks = checked.get('checks', [])
    indexes = [c.get('index') for c in checks]
    complete = len(indexes) == len(levels) and sorted(indexes) == list(range(len(levels)))
    check_map = {c.get('index'): c for c in checks}
    segments = {s['id'] for s in snapshot['transcript']['segments']}
    frames = {f['timestamp_seconds'] for f in snapshot['frames']}
    result = []
    for i, source in enumerate(levels):
        level = dict(source)
        issues = list(check_map.get(i, {}).get('issues', []))
        refs = level.get('evidence_segment_ids') or []
        if not refs or any(ref not in segments for ref in refs):
            issues.append('Transcript references missing or unknown')
        if level.get('frame_seconds') not in frames:
            issues.append('Original numeric frame missing')
        for field in ('currency', 'price_unit', 'source_date', 'adjustment', 'source_horizon', 'conditions'):
            if level.get(field) is None:
                issues.append('Unresolved ' + field)
        try:
            level['symbol'] = symbol_value(level.get('symbol'))
        except ValueError:
            issues.append('Invalid symbol')
        level.update(id=stable_id(snapshot['id'], i, source), evidence_refs=[snapshot['id']],
            verified=complete and check_map.get(i, {}).get('verified') is True and not issues,
            issues=issues)
        result.append(level)
    return result


def _assess_plan(plan, review, evidence_ids, fundamentals):
    issues = list(review.get('issues', []))
    if review.get('accepted') is not True:
        issues.append('Independent review not accepted')
    known = set(evidence_ids)
    refs = plan.get('evidence_refs') or []
    if not refs or not set(refs).issubset(known):
        issues.append('Unknown plan evidence references')
    targets = plan.get('targets') or []
    if len(targets) < 2:
        issues.append('Base/downside scenarios required')
    for target in targets:
        try:
            assumptions = target['assumptions']
            instrument = fundamentals.get('identity') or []
            names = [row.get('name_en', '') for row in instrument if isinstance(row, dict)] if isinstance(instrument, list) else []
            is_etf = plan.get('symbol') in {'SOXX', 'SMH', 'SPY', 'QQQ', 'IWM'} or any('ETF' in name for name in names)
            if is_etf:
                raise ValueError('ETF targets require verified constituent/NAV research; stock EPS multiple is unsupported')
            if target['method'] != 'eps_multiple' or not isinstance(assumptions, dict):
                raise ValueError('Unsupported target method')
            if decimal_value(assumptions['eps']) * decimal_value(assumptions['multiple']) != decimal_value(target['price']):
                raise ValueError('Target arithmetic mismatch')
            if assumptions.get('currency') != plan.get('currency') or assumptions.get('price_unit') != plan.get('price_unit'):
                raise ValueError('Target unit/currency mismatch')
            if not assumptions.get('forecast_period') or not assumptions.get('rationale'):
                raise ValueError('Missing explicit forecast assumptions')
            for value in (target.get('evidence_refs'), assumptions.get('evidence_refs')):
                if not value or not set(value).issubset(known):
                    raise ValueError('Unknown scenario evidence references')
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(str(exc))
    checks = review.get('checks') or {}
    plan['verification'] = {field: checks.get(field) is True for field in ('provenance', 'currency', 'unit', 'adr')}
    # SDK identity/conversion is never inferred from the model's assurance.
    if plan.get('is_adr') or plan.get('price_unit') == 'per_adr' or plan.get('symbol') in {'TSM', 'BABA'}:
        plan['is_adr'] = True
        plan['verification']['adr'] = bool(fundamentals.get('adr_conversion_verified') is True and checks.get('adr') is True)
    if not fundamentals.get('financial_report'):
        issues.append('Financial report unavailable')
    issues.extend(validate_plan(plan))
    return list(dict.fromkeys(issues))


def plan_fingerprint(plan):
    return stable_id({k: v for k, v in plan.items()
        if k not in {'status', 'review_evidence_id', 'review_issues'}})


def plan_evidence_refs(plan):
    refs = set(plan.get('evidence_refs') or [])
    for target in plan.get('targets') or []:
        refs.update(target.get('evidence_refs') or [])
        assumptions = target.get('assumptions') or {}
        if isinstance(assumptions, dict):
            refs.update(assumptions.get('evidence_refs') or [])
    return sorted(refs)


def seal_plan_review(store, plan, review, evidence_ids=None):
    """Bind an explicit independent/manual review to the exact plan contents."""
    refs = plan_evidence_refs(plan)
    evidence_hashes = {}
    for ref in refs:
        value = next((store.get(kind, ref) for kind in ('research_evidence', 'fundamental_evidence',
            'research_level', 'external_evidence') if store.get(kind, ref)), None)
        if value:
            evidence_hashes[ref] = stable_id(value)
    accepted = (review.get('accepted') is True and not plan.get('review_issues')
        and len(evidence_hashes) == len(set(refs))
        and (evidence_ids is None or set(refs).issubset(set(evidence_ids))))
    record = {'plan_hash': plan_fingerprint(plan), 'plan_id': plan['id'], 'revision': plan['revision'],
        'accepted': accepted, 'review': review, 'evidence_refs': refs, 'evidence_hashes': evidence_hashes}
    key = stable_id('sealed-plan-review', record)
    store.put('research_plan_review', key, dict(record, id=key), immutable=True)
    plan['review_evidence_id'] = key
    return key


def _persist_plan(store, plan):
    with store.transaction():
        previous = store.get('plan', plan['id'])
        if previous and previous['revision'] >= plan['revision']:
            raise ValueError('Plan revision must advance')
        store.put('plan_revision', stable_id(plan['id'], plan['revision']), plan, immutable=True)
        store.put('plan', plan['id'], plan)
        if previous:
            transition_id = stable_id('superseded', previous['id'], previous['revision'], plan['revision'])
            store.append_event('plan_lifecycle', transition_id, {'id': transition_id,
                'plan_id': plan['id'], 'old_revision': previous['revision'], 'new_revision': plan['revision'],
                'status': 'superseded', 'timestamp': plan['created_at']})
            for key, intent in store.items('advice_intent'):
                if intent.get('plan_id') == plan['id'] and intent.get('status') in {'pending', 'suspended'}:
                    store.put('advice_intent', key, dict(intent, status='cancelled', reason='superseded_revision'))
            for key, reservation in store.items('reservation'):
                if reservation.get('plan_id') == plan['id'] and reservation.get('status') == 'active':
                    store.put('reservation', key, dict(reservation, status='released', reason='superseded_revision'))


def register_external_evidence(store, path, *, source_url, as_of, reviewed_by):
    """Explicit reviewed primary-source import; no paid lookup or invented ADR facts."""
    if not source_url.startswith('https://') or not reviewed_by:
        raise ValueError('A primary-source HTTPS URL and reviewer are required')
    parse_time(as_of)
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Reviewed source must be a compact text/JSON evidence file')
    content = path.read_text(encoding='utf-8')
    record = {'sha256': _sha(path), 'source_url': source_url, 'as_of': as_of,
        'reviewed_by': reviewed_by, 'content': content}
    key = stable_id('external-primary-source', record)
    store.put('external_evidence', key, dict(record, id=key), immutable=True)
    return key


def bind_instrument_identity(store, symbol, metadata, *, evidence_refs, reviewed_by, reviewed_at):
    symbol = symbol_value(symbol)
    parse_time(reviewed_at)
    if not reviewed_by or not evidence_refs or not all(store.get('external_evidence', ref) for ref in evidence_refs):
        raise ValueError('Explicit reviewed primary-source evidence required')
    if metadata.get('is_adr'):
        if decimal_value(metadata.get('ordinary_shares_per_adr')) <= 0 or not metadata.get('underlying_currency'):
            raise ValueError('Verified ordinary-shares ratio and underlying currency required')
        if metadata.get('currency') != 'USD' or metadata.get('price_unit') != 'per_adr':
            raise ValueError('ADR quote currency/unit mismatch')
    record = {'symbol': symbol, 'metadata': dict(metadata), 'evidence_refs': list(evidence_refs),
        'reviewed_by': reviewed_by, 'reviewed_at': reviewed_at}
    record['id'] = stable_id('instrument-identity', record)
    store.put('instrument_identity_revision', record['id'], record, immutable=True)
    store.put('instrument_identity', symbol, record)
    return record


def compile_plan(store, snapshot, levels, model, fundamentals=None, now=None, context=None, operation_id=None):
    moment = now or utc_now()
    symbols = sorted({l['symbol'] for l in levels if re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', str(l.get('symbol', '')))})
    plans = []
    for symbol in symbols:
        plan_id = stable_id('investment-plan', symbol)
        previous = store.get('plan', plan_id)
        published_at = _published_at(snapshot['metadata'])
        if previous and previous.get('source_published_at') and parse_time(previous['source_published_at']) > parse_time(published_at):
            continue
        fundamental = dict((fundamentals or {}).get(symbol) or {})
        identity = store.get('instrument_identity', symbol)
        if identity:
            fundamental = dict(fundamental, reviewed_identity=identity,
                adr_conversion_verified=bool(identity['metadata'].get('is_adr')
                    and identity['metadata'].get('ordinary_shares_per_adr')))
        fid = stable_id('fundamentals', _stable_fundamentals(fundamental))
        operation_key = stable_id('plan-operation', operation_id or stable_id(snapshot['id'], fid, context), symbol)
        committed = store.get('plan_operation', operation_key)
        if committed:
            plans.append(committed['plan'])
            continue
        attempt = store.get('plan_attempt', operation_key)
        if not attempt:
            attempt = {'timestamp': moment, 'revision': previous['revision'] + 1 if previous else 1}
            store.put('plan_attempt', operation_key, attempt, immutable=True)
        plan_moment = attempt['timestamp']
        observed = store.get('fundamental_evidence', fid)
        if observed:
            fundamental = observed['data']
        else:
            store.put('fundamental_evidence', fid, {'id': fid, 'data': fundamental,
                'first_observed_at': moment}, immutable=True)
        evidence_ids = [snapshot['id'], fid] + [l['id'] for l in levels if l.get('symbol') == symbol and l.get('verified')]
        payload = {'instruction': COMPILE, 'symbol': symbol, 'levels': [l for l in levels if l.get('symbol') == symbol],
            'fundamentals': fundamental, 'evidence_ids': evidence_ids, 'as_of': plan_moment,
            'review_context': context}
        proposed = _run(model, 'compile_plan', payload, now=plan_moment).get('plan', {})
        plan = dict(proposed)
        plan.update(symbol=symbol, id=plan_id,
            revision=attempt['revision'], created_at=plan_moment, status='needs_review', source_published_at=published_at)
        plan['next_review_at'] = (parse_time(plan_moment) + timedelta(days=90)).isoformat()
        plan.setdefault('stop', {})
        if isinstance(plan['stop'], dict):
            plan['stop'].setdefault('confirmation', 'daily_close')
        review = _run(model, 'review_plan', {'instruction': REVIEW, 'plan': plan,
            'source': snapshot, 'levels': levels, 'fundamentals': fundamental}, now=plan_moment)
        issues = _assess_plan(plan, review, evidence_ids, fundamental)
        if not any(l.get('verified') and l.get('symbol') == symbol for l in levels):
            issues.append('No individually verified source levels')
        plan['status'] = 'active' if not issues else 'needs_review'
        plan['review_issues'] = issues
        plan['research_snapshot_id'] = snapshot['id']
        plan['fundamental_evidence_id'] = fid
        seal_plan_review(store, plan, review, evidence_ids)
        with store.transaction():
            _persist_plan(store, plan)
            store.put('plan_operation', operation_key, {'plan': plan}, immutable=True)
        plans.append(plan)
    return plans


def _stable_fundamentals(value):
    if isinstance(value, dict):
        return {key: _stable_fundamentals(item) for key, item in value.items()
                if key != 'fetched_at' and not (key == 'as_of' and 'financial_report' in value)}
    if isinstance(value, list):
        return [_stable_fundamentals(item) for item in value]
    return value


def ingest_archives(store, archive_db, *, model=None, symbols=None, fundamentals=None, now=None, research_since=None):
    moment = now or utc_now()
    cutoff = parse_time(research_since) if research_since is not None else None
    result = {'ingested': 0, 'unchanged': 0, 'errors': [], 'plans': [], 'deferred_before_since': [],
              'research_since': cutoff.isoformat() if cutoff else None}
    with contextlib.closing(sqlite3.connect(Path(archive_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT video_id,revision,source,title,payload,analysis_sha256 FROM analyses ORDER BY video_id,revision').fetchall()
    for row in sorted(rows, key=_row_order):
        try:
            snapshot = _snapshot(row, moment)
            store.put('research_archive_seal', stable_id(row[0], row[1], row[2]), {
                'snapshot_id': snapshot['id'], 'analysis_sha256': snapshot['analysis_sha256'],
                'manifest_sha256': snapshot['manifest_sha256']}, immutable=True)
            existing = store.get('research_evidence', snapshot['id'])
            if existing:
                snapshot = existing
                result['unchanged'] += 1
            else:
                store.put('research_evidence', snapshot['id'], snapshot, immutable=True)
                result['ingested'] += 1
            if cutoff is not None and parse_time(_published_at(snapshot['metadata'])) < cutoff:
                result['deferred_before_since'].append({'video_id': snapshot['video_id'],
                    'revision': snapshot['revision'], 'published_at': _published_at(snapshot['metadata']),
                    'evidence_id': snapshot['id']})
                continue
            identities = {key: value['id'] for key, value in store.items('instrument_identity')}
            workflow_id = stable_id(snapshot['id'], 'workflow-v2', sorted(symbols or []),
                _stable_fundamentals(fundamentals or {}), identities)
            completed = store.get('research_workflow', workflow_id)
            if model is None or completed:
                if completed:
                    result['plans'].extend(completed['plans'])
                continue
            attempt = store.get('research_attempt', workflow_id)
            if not attempt:
                attempt = {'timestamp': moment}
                store.put('research_attempt', workflow_id, attempt, immutable=True)
            workflow_moment = attempt['timestamp']
            extraction_key = stable_id('source-extraction-v2', snapshot['id'],
                getattr(model, 'config', {}).get('model'), getattr(model, 'config', {}).get('base_url'))
            extraction = store.get('research_extraction', extraction_key)
            if extraction:
                levels = extraction['levels']
            else:
                levels = []
                frames = _frames(snapshot)
                for start in range(0, len(frames), 8):
                    batch = frames[start:start + 8]
                    batch_source = dict(snapshot, frames=snapshot['frames'][start:start + 8])
                    extracted = _run(model, 'extract_levels', {'instruction': EXTRACT,
                        'source': batch_source, 'provided_frame_seconds': [s for s, _ in batch]}, batch, workflow_moment)
                    checked = _run(model, 'verify_levels', {'instruction': VERIFY, 'levels': extracted,
                        'source': batch_source}, batch, workflow_moment)
                    levels.extend(_verify_levels(batch_source, extracted, checked))
                levels = list({l['id']: l for l in sorted(levels, key=lambda item: bool(item.get('verified')))}.values())
                store.put('research_extraction', extraction_key, {'levels': levels}, immutable=True)
            for level in levels:
                store.put('research_level', level['id'], level, immutable=True)
            if symbols:
                levels = [l for l in levels if l.get('symbol') in symbols]
            plans = compile_plan(store, snapshot, levels, model, fundamentals, workflow_moment, operation_id=workflow_id)
            store.put('research_workflow', workflow_id, {'id': workflow_id, 'plans': plans,
                'level_ids': [l['id'] for l in levels], 'completed_at': workflow_moment}, immutable=True)
            result['plans'].extend(plans)
        except Exception as exc:
            result['errors'].append({'video_id': row[0], 'revision': row[1], 'error': type(exc).__name__,
                                     'detail': str(exc) if isinstance(exc, ValueError) else 'Research workflow blocked; inspect durable receipts'})
    return result


def review_plan(store, plan, model, *, event_id, evidence=None, now=None):
    """An explicit risk/quarterly event triggers once; routine quote ticks do not."""
    key = stable_id('plan-review', plan['id'], plan['revision'], event_id, _stable_fundamentals(evidence or {}))
    cached = store.get('plan_review', key)
    if cached:
        return cached
    moment = now or utc_now()
    attempt = store.get('review_attempt', key)
    if not attempt:
        prior_fundamental = store.get('fundamental_evidence', plan.get('fundamental_evidence_id', '')) or {}
        latest = store.get('fundamental_cache', plan['symbol']) or {}
        if (evidence or {}).get('fundamentals'):
            data = evidence['fundamentals']
        elif isinstance(evidence, dict) and 'financial_report' in evidence:
            data = {plan['symbol']: evidence}
        else:
            data = {plan['symbol']: latest.get('data') or prior_fundamental.get('data', {})}
        attempt = {'timestamp': moment, 'fundamentals': data, 'evidence': evidence}
        store.put('review_attempt', key, attempt, immutable=True)
    moment, evidence, data = attempt['timestamp'], attempt['evidence'], attempt['fundamentals']
    result = _run(model, 'event_review', {'instruction': REVIEW, 'plan': plan,
        'event_id': event_id, 'new_evidence': evidence}, now=moment)
    snapshot = store.get('research_evidence', plan.get('research_snapshot_id', ''))
    revised = None
    if snapshot:
        levels = [v for _, v in store.items('research_level') if snapshot['id'] in v.get('evidence_refs', [])
                  and v.get('symbol') == plan['symbol']]
        compiled = compile_plan(store, snapshot, levels, model, data, moment,
            context={'prior_plan': plan, 'event_id': event_id, 'new_evidence': evidence, 'event_review': result},
            operation_id=key)
        revised = next((p for p in compiled if p['symbol'] == plan['symbol']), None)
    if revised is None:
        # Missing original evidence cannot be renewed by a superficial review assurance.
        result = dict(result, accepted=False, issues=list(result.get('issues', [])) + ['Original sealed research snapshot unavailable'])
    record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'],
        'event_id': event_id, 'review': result, 'revised_plan': revised,
        'status': revised['status'] if revised else 'needs_review'}
    store.put('plan_review', key, record, immutable=True)
    return record
