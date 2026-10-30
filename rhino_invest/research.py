"""Read sealed video revisions and produce separately reviewed long-term plans."""
from __future__ import annotations

import contextlib
import calendar
import hashlib
import itertools
import json
import math
import re
import sqlite3
from pathlib import Path
from datetime import timedelta, datetime, timezone
from decimal import Decimal, localcontext, InvalidOperation
from .common import utc_now, stable_id, validate_research_plan, classify_plan_validation, decimal_value, parse_time, evidence_limited_no_buy
from .market import symbol_value, instrument_contract, normalize_fundamentals


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


def _latest_revisions(rows):
    versions = {}
    for row in rows:
        versions.setdefault(row[0], []).append(row[1])
    return {video: (values[0] if len(values) == 1 else max(values, key=int)
                    if all(re.fullmatch(r'[0-9]+', value) for value in values) else None)
            for video, values in versions.items()}


def _block_old_revision_plans(store, video_id):
    """A damaged/ambiguous latest seal cannot leave an older revision actionable."""
    for plan_id, plan in store.items('plan'):
        evidence = store.get('research_evidence', plan.get('research_snapshot_id', '')) or {}
        if evidence.get('video_id') != video_id or plan.get('status') in {'superseded', 'invalidated'}:
            continue
        reason = 'latest_sealed_revision_unavailable_requires_review'
        issues = list(dict.fromkeys(list(plan.get('review_issues', [])) + [reason]))
        store.put('plan', plan_id, dict(plan, status='needs_review', review_issues=issues))
        for key, intent in store.items('advice_intent'):
            if intent.get('plan_id') == plan_id and intent.get('status') in {'pending', 'suspended'}:
                store.put('advice_intent', key, dict(intent, status='cancelled', reason=reason))
        for key, reservation in store.items('reservation'):
            if reservation.get('plan_id') == plan_id and reservation.get('status') == 'active':
                store.put('reservation', key, dict(reservation, status='released', reason=reason))


EXTRACT = '''Extract individual price/valuation/stop/target levels only from the supplied
complete sealed transcript and this batch of original frames. Return {"levels":[{"symbol":"TSM",
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
COMPILE_TEMPLATE = {'plan': {'symbol': None, 'thesis': None, 'horizon_months': None,
    'next_review_at': None, 'thesis_valid_from': None, 'horizon_until': None,
    'currency': None, 'price_unit': None, 'is_adr': None,
    'entry': {'low': None, 'high': None, 'amount_usd': None, 'basis': None,
              'source_level_ids': [], 'conditions': [], 'condition_mode': None, 'execution_conditions': []},
    'targets': [{'scenario': scenario, 'price': None, 'horizon_months': None, 'method': 'eps_multiple',
        'assumptions': {'eps': None, 'multiple': None, 'currency': None, 'price_unit': None,
            'forecast_period': None, 'rationale': None, 'evidence_refs': []}, 'evidence_refs': []}
        for scenario in ('base', 'bear')],
    'take_profit': [{'price': None, 'fraction': None}], 'stop': {'price': None, 'confirmation': 'daily_close'},
    'invalidation_conditions': [], 'time_exit_policy': 'Review at next_review_at and horizon_until; renewal requires a new independently accepted thesis.', 'valuation_basis': {},
    'conditions_parsed': False, 'evidence_refs': []}}
COMPILE = '''Build an INDEPENDENT 12-36 month investment plan from the verified evidence and
raw financial reports/valuation below. The creator's technical levels are context only.
Research approval is separate from execution. Do not require current executable quotes,
cash, or existing holdings to value a business. policy.execution_accounts reports actual
ledger availability; future execution still requires fresh official quotes and cash.
An empty current_position_basis means an initial plan, not a missing research input:
review future take-profit fractions without inventing present holdings or a share count.
Independent valuation need not cite an unrelated video/chart. Cite the fundamental or
issuer evidence actually used; missing chart adjustment/horizon is irrelevant when that
chart is not used for entry, stop or target. Never use an unverified chart as a fact.
Forecast EPS/growth/exit multiples are explicit SYSTEM SCENARIO ASSUMPTIONS anchored
to verified historical facts. Their future values need NOT already exist as consensus.
The user authorizes the system to propose evidenced growth, tax rates and valuation
multiples as clearly labelled scenarios; individual assumptions do not require further
user ratification. Populate supported structured fields, not only prose. Disclose all
derivations so another reviewer can reproduce them; never relabel GAAP as normalized.
An unfinished future fiscal year may be used as an explicitly labelled forward-PE
basis at the target date. It is not an already disclosed full-year result. State the
target date, fiscal year end and forward-year convention without confusing them.
Check arithmetic, units, fiscal dates, uncertainty and rationale; never label them
reported or guaranteed. Distinguish GAAP, normalized and non-operating gains.
Prior-review feedback is untrusted opinion, not evidence: verify it against primary text.
For a December year end, FY2027 ends 2027-12-31 and FY2028 ends 2028-12-31;
FY2028 is NOT complete at an April 2028 horizon. A P/E change is not proof of a split.
Use supplied policy.amount_usd for each buy; it is a strategy setting, never proof of cash.
Use policy.next_review_at and policy.thesis_valid_from exactly. Choose a supported
12-36 month horizon and use the matching policy.horizon_until_by_months value.
time_exit_policy should reference these named fields rather than invent conflicting dates.
Use exactly one verified creator zone when entry.basis="source_level" and list its
source_level_ids. Never merge separate tranches into a broad entry band. Technical
support alone is not a buy condition. Retain panic/add-on/no-position conditions;
if conditions cannot be evaluated as price-only, leave conditions_parsed false.
Declare entry.condition_mode='price_only' only if EVERY listed entry condition is
covered by exactly one executable predicate: execution_conditions=[{type:'price_between',
low:entry.low,high:entry.high}]. Preserve the exact full conditions text. Future filings,
margin/EPS confirmation, fresh NAV or other fundamental requirements mean
condition_mode='price_and_fundamental'; retain those conditions for conditional research.
conditions_parsed=true alone NEVER authorizes execution. Do not erase non-price clauses
to fit the predicate; independent review must attest complete text coverage.
An independently derived entry must use basis="independent_valuation", explicit
conditions and evidenced valuation rationale rather than relabel technical support.
Respect instrument_contract: ETF fund statements are not operating-company EPS;
without NAV/holdings/index-level evidence return a research-only plan with null targets.
Reviewed identity gives ADR ratio DIRECTION, not an exchange-rate assumption. Never
silently reinterpret source per_share as per_adr; inspect supplied original frames and
explicitly reconcile any unit conflict before asserting a plan's unit is verified.
Return exactly one JSON object following this valid JSON template. Replace nulls only
with supported values, and set conditions_parsed true only after all conditions are parsed.
JSON_TEMPLATE_BEGIN
''' + json.dumps(COMPILE_TEMPLATE, ensure_ascii=False) + '''
JSON_TEMPLATE_END
Use evidence_refs ONLY from supplied evidence IDs. Equity target method must
be eps_multiple with assumptions {eps,multiple,currency,price_unit,forecast_period,
rationale,evidence_refs}; target=eps*multiple, at least base/bear scenarios. Forecast EPS
and multiple are explicitly scenario assumptions, never pretend they are disclosed.
When deriving future EPS by compound growth, supply optional eps_bridge={anchor_eps,
annual_growth,years} inside assumptions. Supply explicit numeric inputs and explain the
anchor period, forecast period, units and evidence in rationale. The calculator uses
only these structured author inputs; it never guesses a bridge from prose.
For an ETF with verified aligned issuer NAV/portfolio PE/holdings, use etf_nav_scenario
with assumptions {nav_per_share,current_pe,annual_earnings_growth,exit_pe,years,
fees_treatment,rationale,currency,price_unit,evidence_refs}. years=horizon_months/12;
price=nav_per_share*(1+annual_earnings_growth)^years*(exit_pe/current_pe).
Growth and exit PE are explained scenario assumptions based on holdings/industry risks,
not corporate EPS or promises. State fees, concentration and corporate-action basis.
Leave unsupported fields null and state gaps; missing data means needs_review, not a buy.
Do not use illustrative user numbers 370/800/270. No generic automatic upside percentage.'''
COMPILE += '''
For operating-income-derived equity scenarios prefer assumptions.operating_eps_bridge
{operating_income,annual_growth,years,tax_rate,after_tax_nonoperating_cost,diluted_shares}.
EPS=(operating_income*(1+annual_growth)^years*(1-tax_rate)-after_tax_nonoperating_cost)/diluted_shares.
Use consistent income/share scales, explicit source anchors and labelled future assumptions.
Never also supply eps_bridge or compound an already-forecast EPS a second time.
Every initial-entry take-profit must exceed entry.high and stop must be below entry.low.
A NO-BUY decision is valid research: research_intent=no_buy, low/high/stop.price=null,
take_profit=[], with explicit source-backed reasoning and preserved genuine conditions.
If valuation cannot be established, use valuation_basis.kind=insufficient_valuation,
targets=[], and explain the exact evidence gaps; do not fabricate EPS, a target or buy band.
No-buy research never authorizes execution and does not certify a valuation.
'''
REVIEW = '''Independently review each target scenario, arithmetic, explicit assumptions,
financial source periods/units, ADR conversion, downside, entry/stop/TP, thesis invalidation
and 12-36 month horizon. Return {"accepted":false,"issues":[],"checks":{
"provenance":false,"currency":false,"unit":false,"adr":false}}. Only accept if all
material fields have evidence; ambiguous currency/units/ADR forbid activation.
Review research/plan validity, not today's order eligibility. Cash, quote freshness and
absent existing holdings belong to execution gates and notes, not research rejection.
Historical quoted anchors need dates, not a currently executable spread. An initial
TP fraction schedule can be reviewed without current_position_basis.
An independent valuation can use issuer/provider facts without any creator price level;
do not demand a video's horizon/adjustment if its prices are not used in this plan.
Scenario forecasts are explicitly uncertain authored assumptions, not consensus facts:
require evidence for historical anchors, dimensions, arithmetic and defensible rationale,
not an already published future EPS. Reject unsupported/inconsistent assumptions.
Explicit system-authored growth/multiple/tax scenarios do not need separate user
ratification. Require evidenced anchors and a reproducible bridge, not an approval
that the user never reserved. A later fiscal year is a permitted explicitly labelled
forward-PE basis, not evidence that the unfinished fiscal year has been reported.
Use evidence_bundle original excerpts and raw paths. Prior review guesses are not facts:
check fiscal end dates and non-operating gains; never infer a split from a P/E drop.
Put only blocking research/plan objections in issues; execution-only gaps go in notes.
For ETF method check issuer NAV/current PE alignment, freshness, holdings/fees,
corporate actions, years and exact formula. Never apply operating-company EPS to a fund.
The permitted ETF authored scenario uses E0=NAV/current_PE as an implied portfolio
earnings-per-ETF-share proxy, NOT reported company EPS. E_future=E0*(1+growth)^years,
target=E_future*exit_PE, algebraically NAV*(1+growth)^years*(exit_PE/current_PE).
Growth is an explicit portfolio per-share earnings scenario assumption, not an issuer
forecast or a claim that NAV itself is earnings. Check its rationale and fee treatment.
calculation_provenance records deterministic arithmetic only: independently judge all
inputs, source units and economic reasoning. Correct calculation never mandates acceptance.
Read supplied original images, not just frame metadata. Verify EPS growth arithmetic,
forecast fiscal years versus holding horizon, exact entry-zone bounds/conditions and
stop-edge attribution. Review the supplied policy dates and authoritative per-buy amount.
If current_position_basis is supplied, return position_basis_accepted:true ONLY after
independently reviewing the TP schedule against that exact current holdings structure.
Also return entry_condition_review={condition_mode:'price_only'|'price_and_fundamental',
all_text_conditions_covered:false,conditions:[ALL exact original entry conditions],
execution_conditions:[the exact executable predicates]}. Attest price_only and
all_text_conditions_covered=true ONLY if the single exact price_between predicate
covers every text condition. Future fundamental conditions remain conditional research;
do not presume a boolean conditions_parsed proves that they are satisfied.'''
REVIEW += '''
Explicit research_intent=no_buy is a decision to abstain. Require no_buy_reviewed:true
only after independent source/condition review. It never authorizes execution.
For valuation_basis.kind=insufficient_valuation with no entry/targets/stop/TP, review
whether the historical facts and disclosed valuation gaps justify abstaining; do not
force invented forecasts or claim that a point valuation has passed. Empty targets
are allowed ONLY for this explicit non-executable, evidence-limited no-buy decision.
'''
EVENT_REVIEW = REVIEW + ''' For a risk event, also return assessment:{triggered:false,
condition:null,evidence_refs:[],rationale:null}. Set triggered true ONLY when the new
persisted raw evidence proves one EXACT existing plan.invalidation_conditions string.
Do not invent a new condition or treat price weakness alone as thesis invalidation.'''
VERIFY_INVALIDATION = '''Independently verify whether the raw new evidence proves the
assessment.condition, which must EXACTLY equal an existing plan.invalidation_conditions
string. Return {"accepted":false,"condition":null,"evidence_refs":[],"issues":[]}.
Accept only with the same original persisted evidence_refs as the assessment; do not
treat the first model's assertion as evidence. No trade or invented new condition.'''
COMPILE_FORMAT_REGENERATION = '''
FORMAT_REGENERATION_ATTEMPT=compile-json-v1
A previous completed response had unusable output format. Generate the entire plan
again from the SAME original evidence and original images. Return one compact, valid
JSON object following the original schema, with correctly nested braces and commas.
Do not reconstruct, quote, repair or rely on the previous output. Preserve uncertainty,
source references and all required fields; do not invent numbers to complete the JSON.'''
COMPILE_COMPACT_REGENERATION = '''
FORMAT_REGENERATION_ATTEMPT=compile-json-v2-final
Both prior completed responses failed JSON syntax validation. This is the FINAL
format attempt for these exact facts. Generate from the SAME evidence and images.
Return exactly one JSON object with a plan object. Keep narrative strings concise,
use plain text rather than embedded quotation marks, and put calculations in the
structured numeric fields. Preserve every required field, source reference and
uncertainty. No Markdown, no trailing text, no extra closing braces. Never invent
facts or reuse previous malformed text. The independent review still applies.'''


def _run(model, purpose, payload, frames=(), now=None, store=None):
    payload = dict(payload)
    instruction = payload.pop('instruction', None)
    source = payload.get('source')
    plan = payload.get('plan') or {}
    scope = stable_id('research-logical-call', purpose,
        (source or {}).get('id') or plan.get('research_snapshot_id'), payload.get('symbol') or plan.get('symbol'),
        _logical_fundamentals(payload.get('fundamentals')),
        payload.get('event_id') or payload.get('review_event_id') or (payload.get('review_context') or {}).get('event_id'),
        [second for second, _ in frames])
    if store is not None and purpose in {'compile_plan', 'review_plan'}:
        _guard_plan_scope(store, purpose, payload, frames, scope)
    event=(payload.get('review_context') or {}).get('event_id')
    if store is not None and purpose=='compile_plan' and event and store.get('research_local_draft_replay',event):
        gateway_scope=stable_id('model-logical-call-v1',scope,[(second,_sha(Path(path))) for second,path in frames])
        calls=[key for key,value in store.items('model_call') if value.get('purpose')==purpose and value.get('call_scope')==gateway_scope]
        if len(calls)!=1:
            raise ModelOutputInvalid('Local draft replay requires one uniquely completed original operation')
        call_id=calls[0]
        receipt=store.get('model_receipt',call_id);cached=store.get('model_response',call_id)
        if not receipt or not cached or store.get('model_rejection',call_id):
            raise ModelOutputInvalid('Local draft replay requires the preserved receipt and parsed original response')
        store.put('research_completed_draft_replay',event,{'call_id':call_id,'receipt_hash':stable_id(receipt),
            'output_hash':stable_id(cached['output']),'new_paid_generation':False},immutable=True)
        return json.loads(json.dumps(cached['output']))
    if source:
        payload['source'] = _model_source(source, full_transcript=purpose in {'extract_levels', 'verify_levels'},
                                          levels=payload.get('levels'))
    if 'levels' in payload:
        levels = payload['levels']
        payload['levels'] = ({'levels': [_model_level(v) for v in levels['levels']]}
            if isinstance(levels, dict) else [_model_level(v) for v in levels])
    if 'plan' in payload:
        payload['plan'] = _model_plan(payload['plan'])
    context = payload.get('review_context')
    if isinstance(context, dict):
        context = dict(context)
        if context.get('prior_plan'):
            context['prior_plan'] = _model_plan(context['prior_plan'])
        if context.get('event_review'):
            context['event_review'] = {k: context['event_review'][k] for k in ('accepted', 'issues', 'checks')
                                       if k in context['event_review']}
        payload['review_context'] = context
    from .model import ModelOutputInvalid
    try:
        return model.run(purpose, payload, frames=frames, now=now, instruction=instruction, call_scope=scope)
    except ModelOutputInvalid as exc:
        if getattr(model, 'config', {}).get('reuse_evidence_cycles') and purpose in {'compile_plan', 'review_plan'}:
            raise  # Preserve the receipt; invalid formatting never authorizes another paid cycle.
        if (purpose != 'compile_plan' or not _completed_stop_receipt(
                store or getattr(model, 'store', None), exc)):
            raise
        # This exception guarantees a completed durable receipt. The fixed
        # instruction changes only this request fingerprint; its own receipt or
        # unknown fence permits no further attempt across ticks/restarts/days.
        try:
            return model.run(purpose, payload, frames=frames, now=now,
                instruction=(instruction or '') + COMPILE_FORMAT_REGENERATION, call_scope=scope)
        except ModelOutputInvalid as second:
            if not _completed_stop_receipt(store or getattr(model, 'store', None), second):
                raise
            # Fixed third fingerprint: receipt/unknown gates and daily budget still
            # apply. Replays can never buy a fourth request for this frozen payload.
            return model.run(purpose, payload, frames=frames, now=now,
                instruction=(instruction or '') + COMPILE_COMPACT_REGENERATION, call_scope=scope)


def _completed_stop_receipt(store, error):
    """Require this exact request's durable receipt; never infer known outcome."""
    key = getattr(error, 'call_id', None)
    if store is None or not isinstance(key, str):
        return False
    call = store.get('model_call', key)
    if not call or call.get('purpose') != 'compile_plan' or store.get('model_response', key):
        return False
    receipt = store.get('model_receipt', key)
    choices = (receipt or {}).get('response', {}).get('choices')
    if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)
            and choices[0].get('finish_reason') == 'stop'):
        return False
    try:
        return not isinstance(json.loads(choices[0]['message']['content']), dict)
    except (ValueError, TypeError, KeyError):
        return True


def _guard_plan_scope(store, purpose, payload, frames, scope):
    """Adding images/derived contracts cannot retry an unresolved old plan call."""
    from .model import UnknownModelOutcome
    source, plan = payload.get('source') or {}, payload.get('plan') or {}
    phase = stable_id('research-logical-call', purpose, source.get('id') or plan.get('research_snapshot_id'),
        payload.get('symbol') or plan.get('symbol'), _logical_fundamentals(payload.get('fundamentals')),
        payload.get('event_id') or payload.get('review_event_id') or (payload.get('review_context') or {}).get('event_id'), [])
    hashes = [(second, _sha(Path(path))) for second, path in frames]
    current = stable_id('model-logical-call-v1', scope, hashes)
    legacy_scopes = {stable_id('model-logical-call-v1', phase, [])}
    raw_fundamental = payload.get('fundamentals')
    legacy_phases = {phase}
    candidates = [raw_fundamental]
    if isinstance(raw_fundamental, dict):
        candidates.extend(record['data'] for _, record in store.items('fundamental_evidence')
            if _logical_fundamentals(record.get('data')) == _logical_fundamentals(raw_fundamental))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        old_facts = _legacy_logical_fundamentals(candidate)
        variants = [old_facts]
        if 'consensus_live_verified' in candidate:
            variants.extend(dict(old_facts, consensus_live_verified=flag) for flag in (False, True))
        for facts in variants:
            for seconds, frame_hashes in (([], []), ([second for second, _ in frames], hashes)):
                old_phase = stable_id('research-logical-call', purpose, source.get('id') or plan.get('research_snapshot_id'),
                    payload.get('symbol') or plan.get('symbol'), facts,
                    payload.get('event_id') or payload.get('review_event_id') or (payload.get('review_context') or {}).get('event_id'), seconds)
                legacy_scopes.add(stable_id('model-logical-call-v1', old_phase, frame_hashes))
                if not seconds:
                    legacy_phases.add(old_phase)
    if isinstance(raw_fundamental, dict) and 'consensus_live_verified' in raw_fundamental:
        # The old adapter's live-empty flag was an observation flag, not a new
        # consensus fact. Cover either prior value when migrating that contract.
        for flag in (False, True):
            old_fundamental = dict(_logical_fundamentals(raw_fundamental), consensus_live_verified=flag)
            old_phase = stable_id('research-logical-call', purpose, source.get('id') or plan.get('research_snapshot_id'),
                payload.get('symbol') or plan.get('symbol'), old_fundamental,
                payload.get('event_id') or payload.get('review_event_id') or (payload.get('review_context') or {}).get('event_id'), [])
            legacy_scopes.add(stable_id('model-logical-call-v1', old_phase, []))
    with store.transaction():
        scopes = {current} | legacy_scopes | {record['gateway_scope'] for _, record in store.items('research_call_scope')
            if record.get('logical_scope') in legacy_phases}
        for key, call in store.items('model_call'):
            if (call.get('call_scope') in scopes and not store.get('model_receipt', key)
                    and not store.get('model_rejection', key)):
                raise UnknownModelOutcome('Unresolved logical plan call; original-image or contract changes cannot bypass reconciliation')
        store.put('research_call_scope', current, {'logical_scope': phase, 'gateway_scope': current}, immutable=True)


def _model_level(level):
    fields = ('id', 'symbol', 'kind', 'value', 'low', 'high', 'currency', 'price_unit', 'source_date',
        'adjustment', 'source_horizon', 'timestamp_seconds', 'evidence_segment_ids', 'frame_seconds',
        'conditions', 'uncertainty', 'verified', 'issues', 'evidence_refs')
    return {key: level[key] for key in fields if key in level}


def _model_plan(plan):
    fields = ('id', 'revision', 'symbol', 'status', 'created_at', 'evidence_refs', 'thesis',
        'horizon_months', 'next_review_at', 'currency', 'price_unit', 'is_adr', 'entry', 'targets',
        'take_profit', 'stop', 'invalidation_conditions', 'time_exit_policy', 'valuation_basis',
        'conditions_parsed', 'verification', 'review_issues', 'research_snapshot_id',
        'fundamental_evidence_id', 'source_published_at', 'thesis_valid_from', 'horizon_until',
        'risk_evidence_id', 'policy', 'research_gaps', 'take_profit_position_basis', 'price_basis_valid_until',
        'research_assessment', 'execution_requirements', 'calculation_provenance', 'research_origin', 'entry_condition_review', 'condition_resolution', 'research_intent')
    return {key: plan[key] for key in fields if key in plan}


def _model_source(snapshot, *, full_transcript, levels=None):
    """Transport primary evidence, never archived provider envelopes or reasoning."""
    if snapshot.get('source_type') == 'holding_fundamentals':
        return {key: snapshot[key] for key in ('id', 'source_type', 'symbol', 'title',
            'first_observed_at', 'origin', 'scope') if key in snapshot}
    if isinstance(levels, dict):
        levels = levels.get('levels', [])
    levels = levels or []
    segment_ids = {ref for level in levels for ref in level.get('evidence_segment_ids', [])}
    frame_seconds = {level.get('frame_seconds') for level in levels}
    transcript = snapshot['transcript']
    segments = transcript.get('segments', [])
    selected = segments if full_transcript else [v for v in segments if v['id'] in segment_ids]
    frames = snapshot['frames'] if full_transcript else [v for v in snapshot['frames'] if v['timestamp_seconds'] in frame_seconds]
    transcript_hash = next((v['sha256'] for v in snapshot.get('files', [])
        if v['relative_path'].replace('\\', '/').endswith('/transcript.asr.json')), None)
    return {'id': snapshot['id'], 'video_id': snapshot['video_id'], 'revision': snapshot['revision'],
        'title': snapshot['title'], 'metadata': dict(snapshot['metadata']),
        'source_url': 'https://www.youtube.com/watch?v=' + snapshot['video_id'],
        'analysis_sha256': snapshot['analysis_sha256'], 'manifest_sha256': snapshot['manifest_sha256'],
        'evidence_refs': [snapshot['id']],
        'transcript': {'source': transcript.get('source'), 'audio_sha256': transcript.get('audio_sha256'),
            'sha256': transcript_hash, 'full_segment_count': len(segments),
            'selection': 'complete transcript' if full_transcript else 'all complete segments cited by the included levels; full transcript archived locally',
            'segments': [{key: segment[key] for key in ('id', 'start', 'end', 'text') if key in segment} for segment in selected]},
        'frames': [{key: frame[key] for key in ('timestamp_seconds', 'sha256', 'source_url') if key in frame}
                   for frame in frames]}


def _model_cache_identity(model):
    config = getattr(model, 'config', {})
    return {key: config.get(key) for key in ('base_url', 'model', 'configuration_revision',
                                            'max_tokens', 'thinking', 'reasoning_effort')}


def _frames(snapshot):
    result = []
    for frame in snapshot['frames']:
        path = _safe(Path(snapshot['source']), snapshot['video_id'] + '/' + frame['path'])
        if _sha(path) != frame['sha256']:
            raise ValueError('Original frame changed after sealing')
        result.append((frame['timestamp_seconds'], path))
    return result


def _extraction_layout(store, snapshot):
    key=stable_id('research-extraction-layout-v1',snapshot['id'])
    next_key=stable_id('research-extraction-layout-v2',snapshot['id'])
    latest=store.get('research_extraction_layout',next_key)
    if latest is not None:
        return latest
    existing=store.get('research_extraction_layout',key)
    if existing is not None and existing.get('policy')=='accepted_claim_frames':
        return existing
    # The foreground seed may archive a new source before the research thread
    # reaches it. Evidence existence is not proof that a paid request occurred.
    scopes=set()
    for offset in range(0,len(snapshot['frames']),8):
        batch=snapshot['frames'][offset:offset+8]
        seconds=[f['timestamp_seconds'] for f in batch]
        for purpose in ('extract_levels','verify_levels'):
            logical=stable_id('research-logical-call',purpose,snapshot['id'],None,
                _logical_fundamentals(None),None,seconds)
            scopes.add(stable_id('model-logical-call-v1',logical,[(f['timestamp_seconds'],f['sha256']) for f in batch]))
    started=any(call.get('call_scope') in scopes for _,call in store.items('model_call'))
    if existing is not None and started:
        return existing
    selected={c.get('frame_seconds') for c in snapshot.get('analysis',{}).get('claims',[])}
    selected.discard(None)
    focus=bool(selected) and not started
    layout={'policy':'accepted_claim_frames' if focus else 'legacy_complete_frames',
        'frame_seconds':[f['timestamp_seconds'] for f in snapshot['frames']
                         if not focus or f['timestamp_seconds'] in selected]}
    # Preserve an unissued legacy layout as audit; no paid scope may be reset.
    store.put('research_extraction_layout',next_key if existing is not None else key,layout,immutable=True)
    return layout


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


def _review_issues(value):
    """Keep every reviewer objection auditable, including structured objections."""
    if not isinstance(value, list):
        return ['Independent review issues must be a list; received ' + type(value).__name__]
    issues = []
    for issue in value:
        if isinstance(issue, str):
            issues.append(issue if issue.strip() else 'Independent review contains an empty issue')
        else:
            try:
                rendered = json.dumps(issue, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            except (TypeError, ValueError):
                rendered = 'Unserializable ' + type(issue).__name__
            issues.append('Structured independent review issue: ' + rendered)
    return issues


def _assess_plan(plan, review, evidence_ids, fundamentals, levels=()):
    plan.pop('entry_condition_review', None)
    if isinstance(review.get('entry_condition_review'), dict):
        plan['entry_condition_review'] = json.loads(json.dumps(review['entry_condition_review']))
    issues = _review_issues(review.get('issues'))
    if review.get('accepted') is not True:
        issues.append('Independent review not accepted')
    if plan.get('research_intent')=='no_buy' and review.get('no_buy_reviewed') is not True:
        issues.append('Independent no-buy research review required')
    known = set(evidence_ids)
    refs = plan.get('evidence_refs') or []
    if not refs or not set(refs).issubset(known):
        issues.append('Unknown plan evidence references')
    entry = plan.get('entry') or {}
    primary=fundamentals.get('primary_financials') or {}
    if plan.get('symbol')=='GOOG' and primary.get('class_eps_reconciliation') is not None and plan.get('research_intent')!='no_buy':
        facts=primary.get('facts') or []
        latest=max((f.get('period_end','') for f in facts if f.get('metric')=='eps_diluted' and f.get('period_type')=='quarterly'),default='')
        verified=any(f.get('metric')=='class_diluted_eps' and f.get('eps_scope')=='Class C'
            and f.get('period_end')==latest and f.get('source_verified') is True for f in facts)
        if not verified:issues.append('GOOG current Class C EPS/allocation evidence is required before a buy proposal')
    if entry.get('basis') == 'source_level':
        selected = [level for level in levels if level.get('id') in (entry.get('source_level_ids') or [])]
        if len(selected) != 1 or len(entry.get('source_level_ids') or []) != 1:
            issues.append('Entry must identify exactly one verified source zone; never splice tranches')
        elif (not selected[0].get('verified') or selected[0].get('kind') not in {'creator_entry', 'valuation'}):
            issues.append('Technical context alone is not an evidenced creator entry')
        else:
            try:
                low = selected[0].get('low', selected[0].get('value'))
                high = selected[0].get('high', selected[0].get('value'))
                if decimal_value(low) != decimal_value(entry.get('low')) or decimal_value(high) != decimal_value(entry.get('high')):
                    issues.append('Entry band differs from its single referenced source zone')
            except ValueError:
                issues.append('Entry source zone bounds unresolved')
    elif entry.get('basis') != 'independent_valuation':
        issues.append('Explicit entry basis required: source_level or independent_valuation')
    if entry.get('basis') == 'independent_valuation':
        contract = fundamentals.get('instrument_contract') or {}
        if (contract.get('quote_price_unit') != plan.get('price_unit')
                or contract.get('quote_price_unit') not in {'per_share', 'per_adr'}):
            issues.append('Independent valuation instrument unit lacks verified primary identity')
    if not isinstance(entry.get('conditions'), list) or not entry['conditions']:
        issues.append('Explicit entry conditions required')
    elif entry.get('basis') == 'source_level' and len(selected) == 1:
        if selected[0].get('conditions') not in entry['conditions']:
            issues.append('Exact source entry conditions must be retained; conditional support is not an unconditional buy')
    targets = plan.get('targets') or []
    if len(targets) < 2 and not evidence_limited_no_buy(plan):
        issues.append('Base/downside scenarios required')
    for target in targets:
        try:
            assumptions = target['assumptions']
            instrument = fundamentals.get('identity') or []
            names = [row.get('name_en', '') for row in instrument if isinstance(row, dict)] if isinstance(instrument, list) else []
            is_etf = ((fundamentals.get('instrument_contract') or {}).get('asset_type') == 'etf'
                or plan.get('symbol') in {'SOXX', 'SMH', 'SPY', 'QQQ', 'IWM'} or any('ETF' in name for name in names))
            if is_etf:
                if target['method'] != 'etf_nav_scenario':
                    raise ValueError('ETF targets require verified NAV scenarios; stock EPS multiple is unsupported')
                _validate_etf_target(target, plan, fundamentals, known)
                continue
            if target['method'] != 'eps_multiple' or not isinstance(assumptions, dict):
                raise ValueError('Unsupported target method')
            if abs(decimal_value(assumptions['eps']) * decimal_value(assumptions['multiple']) - decimal_value(target['price'])) > Decimal('.01'):
                raise ValueError('Target arithmetic mismatch')
            if assumptions.get('operating_eps_bridge') is not None:
                if assumptions.get('eps_bridge') is not None:
                    raise ValueError('Choose one EPS bridge; forecast growth cannot be applied twice')
                eps, _ = _operating_eps(assumptions['operating_eps_bridge'])
                if abs(eps-decimal_value(assumptions['eps'])) > Decimal('.000001'):
                    raise ValueError('Operating income/tax/share bridge does not reproduce EPS')
            if assumptions.get('eps_bridge') is not None:
                bridge = assumptions['eps_bridge']
                with localcontext() as precision:
                    precision.prec = 40
                    anchor, growth, years = (decimal_value(bridge[key]) for key in ('anchor_eps', 'annual_growth', 'years'))
                    if anchor <= 0 or growth <= -1 or not 0 < years <= 10:
                        raise ValueError('EPS bridge inputs outside supported calculation domain')
                    if abs(anchor * (1 + growth) ** years - decimal_value(assumptions['eps'])) > Decimal('.000001'):
                        raise ValueError('EPS bridge does not reproduce the scenario EPS')
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
    is_etf_plan = (fundamentals.get('instrument_contract') or {}).get('asset_type') == 'etf'
    if not is_etf_plan and not fundamentals.get('financial_report') and not (fundamentals.get('primary_financials') or {}).get('facts'):
        issues.append('Financial report unavailable')
    identity = fundamentals.get('reviewed_identity') or {}
    metadata = identity.get('metadata') or {}
    for key in ('currency', 'price_unit'):
        if metadata.get(key) and plan.get(key) != metadata[key]:
            issues.append('Plan ' + key + ' differs from reviewed instrument identity')
    issues.extend(validate_research_plan(plan))
    from .remediation import economic_consistency_errors
    issues.extend(economic_consistency_errors(plan))
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


def _nested_refs(value):
    refs = set()
    if isinstance(value, dict):
        if isinstance(value.get('evidence_refs'), list):
            refs.update(ref for ref in value['evidence_refs'] if isinstance(ref, str))
        for item in value.values():
            refs.update(_nested_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_nested_refs(item))
    return refs


def bind_research_supplement(store, symbol, data, *, evidence_refs, verified_by, verified_at):
    """Bind reviewed new facts to immutable primary text, never to model assurance."""
    symbol = symbol_value(symbol)
    parse_time(verified_at)
    if not isinstance(data, dict) or not data or not verified_by or not evidence_refs:
        raise ValueError('Reviewed supplementary facts and primary references are required')
    hashes = {}
    for ref in sorted(set(evidence_refs)):
        original = store.get('external_evidence', ref)
        if (not original or not original.get('source_url', '').startswith('https://')
                or not isinstance(original.get('content'), str)
                or hashlib.sha256(original['content'].encode()).hexdigest() != original.get('sha256')):
            raise ValueError('Supplement source must match persisted primary text SHA-256')
        hashes[ref] = stable_id(original)
    content = {'symbol': symbol, 'data': _stable_fundamentals(data), 'evidence_hashes': hashes}
    key = stable_id('research-supplement-v1', content)
    record = store.get('research_supplement', key)
    if not record:
        record = dict(content, id=key, evidence_refs=sorted(hashes), verified_by=verified_by, verified_at=verified_at)
        store.put('research_supplement', key, record, immutable=True)
    store.put('research_supplement_current', symbol, {'id': key})
    return record


def _current_supplement(store, symbol):
    pointer = store.get('research_supplement_current', symbol) or {}
    record = store.get('research_supplement', pointer.get('id', ''))
    if record:
        for ref, digest in record['evidence_hashes'].items():
            original = store.get('external_evidence', ref)
            if not original or stable_id(original) != digest:
                raise ValueError('Supplement primary evidence changed')
    return record


def _source_claims(value, ref):
    claims = set()
    if isinstance(value, dict):
        if ref in (value.get('evidence_refs') or []):
            claims.update(value[key] for key in ('source_excerpt', 'unit_source_excerpt')
                if isinstance(value.get(key), str) and value[key].strip())
        for part in value.values():
            claims.update(_source_claims(part, ref))
    elif isinstance(value, list):
        for part in value:
            claims.update(_source_claims(part, ref))
    return claims


def _primary_text_transport(text, claims):
    """Exact, auditable evidence projection; never cut an unlocated claim."""
    if len(text) <= 65536 or not claims:
        return {'content': text, 'text_selection': 'complete original text'}
    claims = set(claims)
    if 'NAV per Share' in text and '\nHistorical\n' in text:
        # Spreadsheet text may bind the claim only to its numeric NAV cell.
        # Preserve the literal sheet/column labels and issuer identification too.
        claims.update({'NAV per Share', '\nHistorical\n'})
        if 'iShares Semiconductor ETF' in text:
            claims.add('iShares Semiconductor ETF')
    intervals = []
    for claim in sorted(claims):
        starts = [match.start() for match in re.finditer(re.escape(claim), text)]
        if not starts or len(starts) > 128:
            return {'content': text, 'text_selection': 'complete original; claim location absent or ambiguous'}
        for start in starts:
            # Include complete preceding/following lines for table dates/units.
            begin = text.rfind('\n', 0, start)
            begin = text.rfind('\n', 0, max(0, begin)) + 1
            end = text.find('\n', start + len(claim))
            if end >= 0:
                end = text.find('\n', end + 1)
            intervals.append((begin, len(text) if end < 0 else end))
    merged = []
    for begin, end in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((begin, end))
    if sum(end - begin for begin, end in merged) > 65536:
        return {'content': text, 'text_selection': 'complete original; relevant context exceeds projection limit'}
    return {'text_selection': 'all exact claimed passages with complete neighboring lines; other historical rows remain sealed locally',
        'original_text_char_count': len(text), 'offset_unit': 'zero-based Python Unicode code points; end exclusive',
        'excerpts': [{'start_char': begin, 'end_char': end, 'content': text[begin:end],
            'sha256': hashlib.sha256(text[begin:end].encode()).hexdigest()} for begin, end in merged]}


def _evidence_bundle(store, refs, fundamental_id):
    artifacts = []
    fundamental = (store.get('fundamental_evidence', fundamental_id) or {}).get('data', {})
    for ref in refs:
        for kind in ('external_evidence', 'fundamental_evidence', 'research_level', 'research_evidence'):
            record = store.get(kind, ref)
            if record is None:
                continue
            item = {'id': ref, 'kind': kind, 'record_hash': stable_id(record)}
            if kind == 'external_evidence':
                text = record.get('content')
                if (not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != record.get('sha256')):
                    raise ValueError('Primary evidence text SHA-256 mismatch')
                item.update({key: record.get(key) for key in ('sha256', 'source_url', 'as_of', 'published_at')})
                item.update(_primary_text_transport(text, _source_claims(fundamental, ref)))
            elif kind == 'fundamental_evidence':
                item.update(payload_path='/fundamentals', original_data_hash=stable_id(record['data']),
                    note='Native reports/normalizations appear once at this payload path; retain original JSON pointers.')
            elif kind == 'research_evidence':
                item.update(payload_path='/source', analysis_sha256=record.get('analysis_sha256'),
                    manifest_sha256=record.get('manifest_sha256'))
            else:
                item['payload_path'] = '/levels'
            artifacts.append(item)
            break
    bundle = {'contract': 'research-evidence-v4', 'fundamental_evidence_id': fundamental_id,
        'evidence_ids': list(refs), 'artifacts': artifacts,
        'missing_evidence_refs': sorted(set(refs) - {item['id'] for item in artifacts})}
    bundle['hash'] = stable_id(bundle)
    return bundle


def _operation_evidence_bundle(store, operation_key, refs, fundamental_id):
    """Freeze request ordering; reconcile legacy order only by its exact sealed hash."""
    frozen = store.get('plan_evidence_order', operation_key)
    repair = store.get('research_repair', operation_key) or {}
    if frozen:
        if (set(refs) != set(frozen['evidence_ids'])
                or fundamental_id != frozen['fundamental_evidence_id']):
            raise ValueError('Frozen plan evidence membership changed')
        bundle = _evidence_bundle(store, frozen['evidence_ids'], fundamental_id)
        if bundle['hash'] != frozen['bundle_hash']:
            raise ValueError('Frozen plan evidence content changed')
    else:
        bundle = _evidence_bundle(store, refs, fundamental_id)
        expected = repair.get('evidence_bundle_hash')
        if expected and bundle['hash'] != expected:
            # Legacy sets and fresh-extraction versus cached level lists had different orders.
            # Prefer a frozen review, then known extraction orders, then bounded permutations.
            external = {item['id'] for item in bundle['artifacts'] if item['kind'] == 'external_evidence'}
            level_refs = {item['id'] for item in bundle['artifacts'] if item['kind'] == 'research_level'}
            if len(external) > 6:
                raise ValueError('Legacy evidence order requires explicit reconciliation')
            positions = [index for index, ref in enumerate(refs) if ref in external]
            level_positions = [index for index, ref in enumerate(refs) if ref in level_refs]
            artifacts = {item['id']: item for item in bundle['artifacts']}
            matched = None
            checkpoint = store.get('research_critique_review_input', operation_key)
            checkpoint_refs = ((checkpoint or {}).get('payload') or {}).get('evidence_ids')
            direct_orders = [checkpoint_refs] if checkpoint_refs and set(checkpoint_refs) == set(refs) else []
            level_orders = [tuple(ref for ref in refs if ref in level_refs), tuple(sorted(level_refs))]
            for _, extraction in store.items('research_extraction'):
                order = tuple(level['id'] for level in extraction['levels'] if level['id'] in level_refs)
                if len(order) == len(level_refs) and set(order) == level_refs:
                    level_orders.append(order)
            if math.factorial(len(level_refs)) * math.factorial(len(external)) <= 4096:
                level_orders.extend(itertools.permutations(sorted(level_refs)))
            level_orders = list(dict.fromkeys(level_orders))
            def candidates():
                yield from direct_orders
                for level_order in level_orders:
                    for order in itertools.permutations(sorted(external)):
                        ordered = list(refs)
                        for slots, values in ((positions, order), (level_positions, level_order)):
                            for index, ref in zip(slots, values):
                                ordered[index] = ref
                        yield ordered
            for ordered in itertools.islice(candidates(), 4096):
                candidate = {key: value for key, value in bundle.items() if key != 'hash'}
                candidate.update(evidence_ids=ordered, artifacts=[artifacts[ref] for ref in ordered if ref in artifacts])
                digest = stable_id(candidate)
                if digest == expected:
                    matched = dict(candidate, hash=digest)
                    break
            if matched is None:
                raise ValueError('Legacy evidence content differs from its sealed hash')
            bundle = matched
        store.put('plan_evidence_order', operation_key, {
            'evidence_ids': bundle['evidence_ids'], 'fundamental_evidence_id': fundamental_id,
            'bundle_hash': bundle['hash']}, immutable=True)
    if repair.get('evidence_bundle_hash', bundle['hash']) != bundle['hash']:
        raise ValueError('Frozen evidence differs from the original repair record')
    return bundle


def _typed_research_gaps(issues, review, refs):
    raw = review.get('issues') if isinstance(review.get('issues'), list) else []
    structured = {json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':')): item
                  for item in raw if isinstance(item, dict)}
    gaps = []
    for text in issues:
        original = next((value for key, value in structured.items() if key in text), {})
        lower = text.lower()
        category = next((code for terms, code in [
            (('eps', 'unit'), 'EPS_UNIT_UNVERIFIED'),
            (('verification.adr',), 'ADR_IDENTITY_UNVERIFIED'),
            (('verification.unit',), 'INSTRUMENT_UNIT_UNVERIFIED'),
            (('financial report',), 'FINANCIAL_FACTS_MISSING'),
            (('source level',), 'CHART_ENTRY_EVIDENCE_UNVERIFIED'),
            (('scenario', 'arithmetic'), 'VALUATION_ARITHMETIC_INVALID'),
            (('independent review not accepted',), 'INDEPENDENT_REVIEW_NOT_ACCEPTED'),
        ] if all(term in lower for term in terms)), None)
        code = original.get('code') or category or ('PLAN_VALIDATION_' + stable_id(text)[:12])
        gaps.append({'code': code, 'scope': original.get('scope', 'research_plan'), 'blocking': True,
            'detail': text, 'required_evidence': original.get('required_evidence', []),
            'evidence_refs': original.get('evidence_refs') or list(refs)})
    return gaps


def _date(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date()


def _declared_effective_day(action):
    english = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s*\d{4}'
    match = re.search(r'(?:split-adjusted basis on|(?:becomes )?effective(?: date)?(?: is|:| on)?)\s*('
        + english + r'|\d{4}-\d{2}-\d{2})', str(action.get('source_excerpt', '')), re.I)
    if not match:
        return None
    raw = re.sub(r'\s+', ' ', match.group(1))
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%b %d, %Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def _etf_input_gaps(fundamentals, moment, known_refs, store=None):
    etf = fundamentals.get('etf_valuation') or {}
    gaps = []
    def gap(code, detail):
        gaps.append({'code': code, 'scope': 'etf_valuation', 'blocking': True, 'detail': detail,
            'required_evidence': ['issuer NAV, portfolio PE, holdings, fees and corporate-action basis'], 'evidence_refs': []})
    if etf.get('corporate_action_conflict') is True:
        gap('ETF_CORPORATE_ACTION_CONFLICT', 'Issuer split announcements conflict; reconcile the price and share basis before approval')
    if etf.get('issuer_verified') is not True:
        gap('ETF_ISSUER_EVIDENCE_UNVERIFIED', 'Issuer evidence package has not been verified')
    for name in ('nav', 'pe', 'holdings'):
        item = etf.get(name) or {}
        if not item.get('evidence_refs') or not set(item['evidence_refs']).issubset(known_refs):
            gap('ETF_' + name.upper() + '_SOURCE_MISSING', name + ' lacks persisted primary evidence references')
        elif store is not None:
            from urllib.parse import urlparse
            for ref in item['evidence_refs']:
                original = store.get('external_evidence', ref) or {}
                host = urlparse(original.get('source_url', '')).hostname or ''
                if (not any(host == domain or host.endswith('.' + domain) for domain in ('ishares.com', 'blackrock.com'))
                        or not isinstance(original.get('content'), str)
                        or hashlib.sha256(original['content'].encode()).hexdigest() != original.get('sha256')):
                    gap('ETF_' + name.upper() + '_ISSUER_SOURCE_UNVERIFIED', name + ' raw issuer evidence cannot be validated')
        try:
            age = (parse_time(moment).date() - _date(item.get('as_of'))).days
            if not 0 <= age <= 7:
                raise ValueError()
        except (ValueError, TypeError):
            gap('ETF_' + name.upper() + '_DATE_INVALID', name + ' must have an explicit current issuer date within seven days')
    nav, pe, holdings = etf.get('nav') or {}, etf.get('pe') or {}, etf.get('holdings') or {}
    try:
        if decimal_value(nav.get('value')) <= 0 or decimal_value(pe.get('value')) <= 0:
            raise ValueError()
    except ValueError:
        gap('ETF_BASELINE_VALUE_MISSING', 'Positive issuer NAV and comparable portfolio PE are required')
    if nav.get('currency') != 'USD' or nav.get('price_unit') != 'per_share':
        gap('ETF_NAV_UNIT_UNRESOLVED', 'NAV must explicitly be USD per ETF share')
    if nav.get('as_of') != pe.get('as_of') or nav.get('as_of') != holdings.get('as_of'):
        gap('ETF_BASELINE_DATES_UNALIGNED', 'Use same-date issuer NAV, portfolio PE and holdings; do not silently shift dates')
    if not holdings.get('rows') or holdings.get('coverage') is None or holdings.get('concentration') is None:
        gap('ETF_HOLDINGS_COVERAGE_MISSING', 'Holdings, coverage and concentration must be visible')
    fee = etf.get('expense_ratio') or etf.get('fee') or etf.get('fees') or {}
    try:
        if (not isinstance(fee, dict) or decimal_value(fee.get('value')) < 0
                or fee.get('unit') not in {'percent_per_year', 'fraction_per_year'}
                or not fee.get('evidence_refs') or not set(fee['evidence_refs']).issubset(known_refs)):
            raise ValueError()
        if store is not None:
            for ref in fee['evidence_refs']:
                original = store.get('external_evidence', ref) or {}
                from urllib.parse import urlparse
                host = urlparse(original.get('source_url', '')).hostname or ''
                if (not isinstance(original.get('content'), str)
                        or hashlib.sha256(original['content'].encode()).hexdigest() != original.get('sha256')
                        or not any(host == domain or host.endswith('.' + domain) for domain in ('ishares.com', 'blackrock.com'))):
                    raise ValueError()
    except (ValueError, TypeError):
        gap('ETF_FEES_UNRESOLVED', 'Issuer expense ratio and fee treatment are required')
    for action in etf.get('corporate_actions') or []:
        if action.get('type') != 'split':
            continue
        try:
            declared = _declared_effective_day(action)
            if declared != _date(action.get('effective_at') or action.get('effective_date')):
                raise ValueError()
            if store is not None:
                from .market import _primary_verified
                if not _primary_verified(store, action):
                    raise ValueError()
        except (ValueError, TypeError):
            gap('ETF_CORPORATE_ACTION_DATE_UNVERIFIED', 'Declared split effective date must match its persisted issuer text')
    return gaps


def _validate_etf_target(target, plan, fundamentals, known):
    assumptions = target.get('assumptions') or {}
    etf = fundamentals.get('etf_valuation') or {}
    nav, pe = etf.get('nav') or {}, etf.get('pe') or {}
    if (decimal_value(assumptions.get('nav_per_share')) != decimal_value(nav.get('value'))
            or decimal_value(assumptions.get('current_pe')) != decimal_value(pe.get('value'))):
        raise ValueError('ETF scenario baseline differs from verified issuer NAV/PE')
    years = decimal_value(assumptions.get('years'))
    if abs(years - decimal_value(target.get('horizon_months')) / 12) > Decimal('0.000001'):
        raise ValueError('ETF years must equal horizon_months / 12')
    growth, exit_pe = decimal_value(assumptions.get('annual_earnings_growth')), decimal_value(assumptions.get('exit_pe'))
    if years <= 0 or growth <= -1 or exit_pe <= 0:
        raise ValueError('ETF scenario growth and exit PE must have valid domains')
    try:
        with localcontext() as context:
            context.prec = 40
            price = decimal_value(nav['value']) * (Decimal(1) + growth) ** years * exit_pe / decimal_value(pe['value'])
            if abs(price - decimal_value(target.get('price'))) > Decimal('0.01'):
                raise ValueError('ETF NAV scenario arithmetic mismatch exceeds 0.01 USD')
    except InvalidOperation:
        raise ValueError('ETF NAV scenario arithmetic invalid') from None
    if (assumptions.get('currency') != 'USD' or assumptions.get('price_unit') != 'per_share'
            or plan.get('price_unit') != 'per_share'):
        raise ValueError('ETF NAV scenario unit/currency mismatch')
    if not assumptions.get('fees_treatment') or not assumptions.get('rationale'):
        raise ValueError('ETF fees and holdings/industry scenario rationale required')
    for refs in (target.get('evidence_refs'), assumptions.get('evidence_refs')):
        if not refs or not set(refs).issubset(known):
            raise ValueError('Unknown ETF scenario evidence references')


CALCULATION_CONTRACT = 'scenario-price-decimal-v1'


def _operating_eps(bridge):
    """Reproduce authored fiscal inputs without selecting financial assumptions."""
    keys=('operating_income','annual_growth','years','tax_rate','after_tax_nonoperating_cost','diluted_shares')
    values={key:decimal_value(bridge[key]) for key in keys}
    income,growth,years,tax,cost,shares=(values[key] for key in keys)
    if income<=0 or growth<=-1 or not 0<=years<=10 or not 0<=tax<1 or cost<0 or shares<=0:
        raise ValueError('Operating EPS bridge inputs outside supported calculation domain')
    with localcontext() as precision:
        precision.prec=40
        grown=income*(1+growth)**years
        after_tax=grown*(1-tax)
        net=after_tax-cost
        if net<=0:raise ValueError('Nonpositive operating EPS cannot support this positive-PE method')
        return net/shares,{'grown_operating_income':str(grown),'after_tax_operating_income':str(after_tax),
                           'after_tax_net_proxy':str(net),'diluted_shares':str(shares)}


def _calculate_target_prices(proposal):
    """Calculate disclosed formulas only; never select or repair assumptions."""
    plan = json.loads(json.dumps(proposal))
    rows, mappings = [], {}
    for index, target in enumerate(plan.get('targets') or []):
        if not isinstance(target, dict):
            continue
        assumptions = target.get('assumptions') or {}
        try:
            with localcontext() as precision:
                precision.prec = 40
                intermediates, eps_bridge = {}, None
                if target.get('method') == 'eps_multiple':
                    bridge = assumptions.get('eps_bridge') if isinstance(assumptions, dict) else None
                    operating_bridge=assumptions.get('operating_eps_bridge')
                    if operating_bridge is not None:
                        if bridge is not None:continue
                        derived,details=_operating_eps(operating_bridge)
                        eps_bridge={'inputs':dict(operating_bridge),'original_eps':assumptions.get('eps'),
                            'derived_eps_unrounded':str(derived),'intermediates':details,
                            'formula':'(operating_income * (1 + annual_growth) ** years * (1 - tax_rate) - after_tax_nonoperating_cost) / diluted_shares'}
                        assumptions['eps']=str(derived)
                    if bridge is not None:
                        anchor = decimal_value(bridge['anchor_eps'])
                        growth, years = decimal_value(bridge['annual_growth']), decimal_value(bridge['years'])
                        if anchor <= 0 or growth <= -1 or not 0 < years <= 10:
                            continue
                        factor = (Decimal(1) + growth) ** years
                        derived = anchor * factor
                        eps_bridge = {'inputs': dict(bridge), 'original_eps': assumptions.get('eps'),
                            'growth_factor': str(factor), 'derived_eps_unrounded': str(derived),
                            'formula': 'anchor_eps * (1 + annual_growth) ** years'}
                        assumptions['eps'] = str(derived)
                    eps, multiple = decimal_value(assumptions['eps']), decimal_value(assumptions['multiple'])
                    if eps <= 0 or multiple <= 0:
                        continue
                    price = eps * multiple
                    intermediates = {'eps': str(eps), 'multiple': str(multiple)}
                    formula = 'eps * multiple'
                elif target.get('method') == 'etf_nav_scenario':
                    nav, pe = decimal_value(assumptions['nav_per_share']), decimal_value(assumptions['current_pe'])
                    growth, exit_pe = decimal_value(assumptions['annual_earnings_growth']), decimal_value(assumptions['exit_pe'])
                    years = decimal_value(assumptions['years'])
                    if (nav <= 0 or pe <= 0 or exit_pe <= 0 or growth <= -1 or not 1 <= years <= 3
                            or abs(years - decimal_value(target['horizon_months']) / 12) > Decimal('.000001')):
                        continue
                    implied = nav / pe
                    factor = (1 + growth) ** years
                    grown = implied * factor
                    price = grown * exit_pe
                    intermediates = {'nav_per_share': str(nav), 'current_pe': str(pe),
                        'implied_earnings_proxy_per_etf_share': str(implied), 'growth_factor': str(factor),
                        'grown_earnings_proxy_per_etf_share': str(grown), 'exit_multiple': str(exit_pe),
                        'exit_pe_ratio': str(exit_pe / pe)}
                    formula = '(nav_per_share / current_pe) * (1 + annual_earnings_growth) ** years * exit_pe'
                else:
                    continue
                corrected = str(price.quantize(Decimal('.01')))
            original = target.get('price')
            try:
                old = decimal_value(original)
                changed = abs(old - decimal_value(corrected)) > Decimal('.005')
                mappings.setdefault(old, set()).add(corrected)
            except ValueError:
                changed = True
            target['price'] = corrected
            rows.append({'index': index, 'scenario': target.get('scenario'), 'method': target['method'],
                'formula': formula, 'inputs': assumptions, 'original_price': original,
                'intermediates': intermediates, 'eps_bridge': eps_bridge, 'unrounded_price': str(price),
                'rounding': 'Decimal precision 40, half-even to USD 0.01',
                'calculated_price': corrected, 'changed': changed})
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            continue  # Missing/invalid inputs remain defects for independent review.
    profit_changes = []
    for index, step in enumerate(plan.get('take_profit') or []):
        if not isinstance(step, dict):
            continue
        try:
            matches = mappings.get(decimal_value(step.get('price')), set())
            if len(matches) == 1:
                replacement = next(iter(matches))
                if decimal_value(replacement) != decimal_value(step['price']):
                    profit_changes.append({'index': index, 'original_price': step['price'], 'calculated_price': replacement})
                    step['price'] = replacement
        except (ValueError, TypeError):
            pass
    return plan, {'contract': CALCULATION_CONTRACT, 'rounding': 'USD cents; Decimal precision 40, half-even',
        'proposal_hash': stable_id(proposal), 'targets': rows, 'take_profit_exact_target_mappings': profit_changes,
        'inputs_selected_by_calculator': False, 'entry_and_stop_modified': False}


def _price_basis_deadline(fundamentals, moment, refs, store=None):
    from zoneinfo import ZoneInfo
    etf = fundamentals.get('etf_valuation') or {}
    try:
        nav_day = _date((etf.get('nav') or {}).get('as_of')) if etf.get('nav') else parse_time(moment).date()
    except (ValueError, TypeError):
        return None  # Invalid issuer dates are recorded by the typed ETF input gate.
    deadlines = []
    for action in etf.get('corporate_actions') or []:
        evidence = action.get('evidence_refs') or []
        if not evidence or not set(evidence).issubset(refs):
            continue
        if store is not None:
            from .market import _primary_verified
            if not _primary_verified(store, action):
                continue
        effective = action.get('effective_at') or action.get('effective_date')
        try:
            day = _date(effective)
        except (ValueError, TypeError):
            continue
        if _declared_effective_day(action) != day:
            continue
        if day > nav_day:
            deadlines.append(datetime(day.year, day.month, day.day, tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc))
    return min(deadlines).isoformat() if deadlines else None


def _research_fundamental(store, symbol, raw):
    fundamental = dict(raw or {}, symbol=symbol)
    supplement = _current_supplement(store, symbol)
    if supplement:
        fundamental['research_supplement'] = {'id': supplement['id'], 'data': supplement['data'],
            'evidence_refs': supplement['evidence_refs']}
    fundamental = normalize_fundamentals(fundamental, store=store)
    fundamental['instrument_contract'] = instrument_contract(fundamental, store=store)
    identity = store.get('instrument_identity', symbol)
    if identity:
        fundamental.update(reviewed_identity=identity,
            adr_conversion_verified=bool(identity['metadata'].get('is_adr') and identity['metadata'].get('ordinary_shares_per_adr')))
        fundamental['instrument_contract'] = dict(fundamental['instrument_contract'],
            quote_currency=identity['metadata'].get('currency'), quote_price_unit=identity['metadata'].get('price_unit'),
            adr_direction={'ordinary_shares_per_adr': identity['metadata'].get('ordinary_shares_per_adr'),
                'meaning': 'one ADR represents this many ordinary shares'},
            reviewed_identity_evidence_refs=identity.get('evidence_refs', []))
    return fundamental, identity


def _logical_fundamentals(fundamental):
    # A derived contract projection is not a new financial fact and must never
    # bypass an unresolved paid-call scope from the pre-contract payload.
    if isinstance(fundamental, dict):
        fundamental = {key: value for key, value in fundamental.items()
            if key not in {'instrument_contract', 'consensus_response_received', 'consensus_live_verified',
                           'normalized_financials', 'consensus_normalized', 'reference_quote', 'market_quote',
                           'financial_report_request', 'primary_collection'}}
    return _stable_fundamentals(fundamental)


def _legacy_logical_fundamentals(value):
    """Reconstruct the previous scope from archived inputs, never delete fences."""
    def old_stable(item):
        if isinstance(item, dict):
            return {key: old_stable(part) for key, part in item.items()
                if key != 'fetched_at' and not (key == 'as_of' and 'financial_report' in item)}
        if isinstance(item, list):
            return [old_stable(part) for part in item]
        return item
    if isinstance(value, dict):
        value = {key: part for key, part in value.items()
            if key not in {'instrument_contract', 'consensus_response_received', 'consensus_live_verified'}}
        if isinstance(value.get('consensus_normalized'), dict):
            value['consensus_normalized'] = {key: part for key, part in value['consensus_normalized'].items() if key != 'coverage'}
    return old_stable(value)


def _horizon_end(moment, months):
    start = parse_time(moment)
    if isinstance(months, bool) or not isinstance(months, int) or not 12 <= months <= 36:
        return None
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    return start.replace(year=year, month=month + 1,
        day=min(start.day, calendar.monthrange(year, month + 1)[1])).isoformat()


def _plan_frames(snapshot, levels):
    if snapshot.get('source_type') == 'holding_fundamentals':
        return []
    seconds = {level.get('frame_seconds') for level in levels if level.get('verified') is True}
    frames = [(second, path) for second, path in _frames(snapshot) if second in seconds]
    if len(frames) > 8:
        raise ValueError('Plan has more than eight referenced original frames; explicit batched evidence review required')
    return frames


def _unresolved_thesis_defer(store, plan, snapshot, fundamentals, moment):
    if not plan:
        return None
    from .decisions import valid_thesis_events
    # A frozen workflow clock must not hide risk sealed after its first attempt.
    # A verified risk remains unresolved until a dedicated resolution contract
    # exists; this conservative guard does not automatically resolve even a
    # record whose observation timestamp needs reconciliation.
    risks = valid_thesis_events(store, plan)
    if not risks:
        return None
    raw_hash = stable_id(_logical_fundamentals((fundamentals or {}).get(plan['symbol']) or {}))
    key = stable_id('unresolved-thesis-defer-v1', snapshot['id'], plan['id'], plan['revision'],
        sorted(risk['id'] for risk in risks), raw_hash)
    existing = store.get('research_defer', key)
    if existing:
        return existing
    record = {'id': key, 'code': 'UNRESOLVED_VERIFIED_THESIS_RISK', 'status': 'deferred',
        'timestamp': moment, 'symbol': plan['symbol'], 'source_id': snapshot['id'],
        'plan_id': plan['id'], 'plan_revision': plan['revision'], 'plan_hash': plan_fingerprint(plan),
        'conditions': sorted({risk['condition'] for risk in risks}),
        'risk_evidence_ids': sorted(risk['id'] for risk in risks),
        'original_evidence_hashes': {ref: digest for risk in risks for ref, digest in risk['evidence_hashes'].items()},
        'new_fundamentals_hash': raw_hash, 'original_plan_preserved': True,
        'required_next_step': 'Dedicated independently verified risk-resolution review; no automatic reactivation'}
    store.put('research_defer', key, record, immutable=True)
    return record


CRITIQUE_INSTRUCTION = '''
SEMANTIC_CRITIQUE_ATTEMPT=1
Revise the supplied original economic proposal ONCE using the SAME original evidence,
images and the specific blocking independent review issues. Review issues are opinions,
not new source facts: verify their claims, including arithmetic, against original sources
and the precise calculation book. Repair supported numeric/schema/internal-consistency
defects and disclose all changed author assumptions. Never invent missing source facts,
units or normalization adjustments, lower review standards, or presume acceptance.
Populate structured fields and explicit bridges where supported. Keep unresolved true
evidence gaps explicit. This is a fresh complete proposal, followed by independent review.'''


def _critique_ready(store, plan, fundamental, moment):
    binding = store.get('research_plan_review', plan.get('review_evidence_id', '')) or {}
    review = binding.get('review') or {}
    if (plan.get('status') != 'needs_review' or binding.get('plan_hash') != plan_fingerprint(plan)
            or review.get('accepted') is not False or review.get('deterministic_preflight')
            or not isinstance(review.get('issues'), list) or not review['issues']):
        return False
    contract = fundamental.get('instrument_contract') or {}
    if contract.get('quote_currency') != 'USD' or contract.get('quote_price_unit') not in {'per_share', 'per_adr'}:
        return False
    if contract.get('quote_price_unit') == 'per_adr' and fundamental.get('adr_conversion_verified') is not True:
        return False
    if contract.get('asset_type') == 'etf':
        refs = set(plan_evidence_refs(plan)) | _nested_refs(fundamental)
        return not _etf_input_gaps(fundamental, moment, refs, store)
    return (contract.get('stock_eps_valuation_applicable') is True
        and bool(fundamental.get('financial_report') or (fundamental.get('primary_financials') or {}).get('facts')))


def _record_critique_output_failure(store, key, critique, phase, error):
    if not critique:
        return
    from .model import ModelOutputInvalid
    receipt_id = getattr(error, 'call_id', None)
    if isinstance(error, ModelOutputInvalid) and receipt_id and store.get('model_receipt', receipt_id):
        store.put('research_critique_result', key, {'status': 'output_invalid', 'phase': phase,
            'receipt_id': receipt_id, 'terminal': True, 'semantic_attempts': 1}, immutable=True)


def _critique_schema_errors(proposal):
    """Reject malformed containers, leaving missing economic facts to review."""
    if not isinstance(proposal, dict):
        return ['plan must be an object']
    errors = []
    for field in ('entry', 'stop'):
        if field in proposal and not isinstance(proposal[field], dict):
            errors.append(field + ' must be an object')
    for field in ('targets', 'take_profit'):
        if field in proposal and (not isinstance(proposal[field], list)
                or any(not isinstance(row, dict) for row in proposal[field])):
            errors.append(field + ' must be an array of objects')
    for target in proposal.get('targets', []) if isinstance(proposal.get('targets'), list) else []:
        if isinstance(target, dict) and 'assumptions' in target and not isinstance(target['assumptions'], dict):
            errors.append('target.assumptions must be an object')
    ref_owners = [proposal]
    for target in proposal.get('targets', []) if isinstance(proposal.get('targets'), list) else []:
        if isinstance(target, dict):
            ref_owners.append(target)
            if isinstance(target.get('assumptions'), dict):
                ref_owners.append(target['assumptions'])
    for owner in ref_owners:
        if 'evidence_refs' in owner and (not isinstance(owner['evidence_refs'], list)
                or any(not isinstance(ref, str) for ref in owner['evidence_refs'])):
            errors.append('evidence_refs must be an array of strings')
    return errors


def _critique_superseded(store, key, critique, current):
    if not critique or not current or plan_fingerprint(current) == critique['source_plan_hash']:
        return False
    store.put('research_critique_result', key, {'status': 'superseded', 'terminal': True,
        'semantic_attempts': 1, 'current_plan_id': current['id'],
        'current_revision': current['revision']}, immutable=True)
    return True


def compile_plan(store, snapshot, levels, model, fundamentals=None, now=None, context=None, operation_id=None, deferred=None):
    moment = now or utc_now()
    holding_source = snapshot.get('source_type') == 'holding_fundamentals'
    symbols = [symbol_value(snapshot['symbol'])] if holding_source else sorted({l['symbol'] for l in levels if re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', str(l.get('symbol', '')))})
    plans = []
    for symbol in symbols:
        plan_id = stable_id('investment-plan', symbol)
        previous = store.get('plan', plan_id)
        if (context or {}).get('kind') == 'system-plan-remediation-v1':
            from .remediation import same_published_seed
            if previous and not same_published_seed(previous,context):
                plans.append(previous)
                continue  # A stale repair cannot overwrite a newer or already committed revision.
        risk_defer = _unresolved_thesis_defer(store, previous, snapshot, fundamentals, moment)
        if risk_defer:
            if deferred is not None:
                deferred.append(risk_defer)
            continue
        published_at = None if holding_source else _published_at(snapshot['metadata'])
        if previous and published_at and previous.get('source_published_at') and parse_time(previous['source_published_at']) > parse_time(published_at):
            continue
        fundamental, identity = _research_fundamental(store, symbol, (fundamentals or {}).get(symbol))
        if model is not None and getattr(model, 'config', {}).get('reuse_evidence_cycles'):
            from .reuse import completed_evidence_plan
            reused = completed_evidence_plan(store, snapshot, symbol, fundamental, context, moment)
            if reused is not None:
                plans.append(reused)
                continue
        fid = stable_id('fundamentals', _stable_fundamentals(fundamental))
        if (not context and previous and previous.get('condition_resolution') is not None
                and previous.get('research_snapshot_id') == snapshot['id']
                and previous.get('fundamental_evidence_id') == fid
                and (previous.get('status') == 'active' or
                     (previous.get('research_assessment') or {}).get('status') in ('approved_conditional','approved_no_buy'))):
            from .decisions import _has_provenance
            if _has_provenance(store, previous):
                plans.append(previous)
                continue  # Preserve a reviewed replacement over an old malformed generator cache.
        # Workflow discovery changes with the watch list; paid plans depend only
        # on their own immutable source, relevant facts, model and explicit event.
        event_context = None if not context or not context.get('event_id') else {
            'event_id': context.get('event_id'),
            'new_evidence': _stable_fundamentals(context.get('new_evidence'))}
        operation_key = stable_id('plan-operation-v4-evidence-repair', snapshot['id'], symbol, fid,
                                  _model_cache_identity(model), event_context)
        model_ready = model is not None and getattr(model, 'config', {}).get('enabled', True) is not False
        if holding_source:
            operation_key = stable_id(operation_key, 'holding-research-v1', model_ready)
        calculation_seed = None
        critique = None
        source_operation_key = operation_key
        committed = store.get('plan_operation', operation_key)
        if committed:
            if (previous and previous.get('revision', 0) > committed['plan'].get('revision', 0)
                    and (previous.get('status') == 'active'
                        or previous.get('research_assessment', {}).get('status') in ('approved_conditional','approved_no_buy'))):
                plans.append(previous)
                continue  # A stale rejected operation must not replace an accepted newer thesis.
            _, preview = _calculate_target_prices(committed['plan'])
            if (committed['plan'].get('status') == 'needs_review'
                    and any(row['changed'] for row in preview['targets'])):
                calculation_seed = committed['plan']
                operation_key = stable_id(source_operation_key, CALCULATION_CONTRACT)
                completed_calculation = store.get('plan_operation', operation_key)
                if completed_calculation:
                    committed = completed_calculation
                    calculation_seed = None
            if calculation_seed is None and committed and model_ready and _critique_ready(store, committed['plan'], fundamental, moment):
                operation_key = stable_id('plan-critique-v1', source_operation_key, _logical_fundamentals(fundamental))
                terminal = store.get('research_critique_result', operation_key)
                repaired = store.get('plan_operation', operation_key)
                if repaired or terminal:
                    plans.append(previous if terminal and terminal.get('status') == 'superseded' and previous
                        else (repaired or {}).get('plan') or committed['plan'])
                    continue
                critique = store.get('research_critique_attempt', operation_key)
                if not critique:
                    parent_plan = committed['plan']
                    binding = store.get('research_plan_review', parent_plan['review_evidence_id'])
                    critique = {'id': operation_key, 'base_operation_id': source_operation_key,
                        'source_plan_hash': plan_fingerprint(parent_plan), 'source_plan_revision': parent_plan['revision'],
                        'source_review_id': parent_plan['review_evidence_id'], 'source_review_hash': stable_id(binding),
                        'economic_proposal': {key: parent_plan[key] for key in COMPILE_TEMPLATE['plan'] if key in parent_plan},
                        'blocking_review_issues': binding['review']['issues'], 'review_is_untrusted_opinion': True,
                        'calculation_book': _calculate_target_prices(parent_plan)[1],
                        'timestamp': moment, 'compile_instruction': COMPILE + CRITIQUE_INSTRUCTION,
                        'review_instruction': REVIEW, 'max_semantic_attempts': 1, 'max_paid_requests_including_json_recovery': 5}
                    store.put('research_critique_attempt', operation_key, critique, immutable=True)
            elif calculation_seed is None:
                plans.append(committed['plan'])
                continue
        attempt = store.get('plan_attempt', operation_key)
        if not attempt:
            attempt = {'timestamp': critique['timestamp'] if critique else moment, 'revision': previous['revision'] + 1 if previous else 1}
            store.put('plan_attempt', operation_key, attempt, immutable=True)
        plan_moment = attempt['timestamp']
        observed = store.get('fundamental_evidence', fid)
        if observed:
            fundamental = observed['data']
        else:
            store.put('fundamental_evidence', fid, {'id': fid, 'data': fundamental,
                'first_observed_at': moment}, immutable=True)
        evidence_ids = [snapshot['id'], fid] + [l['id'] for l in levels if l.get('symbol') == symbol and l.get('verified')]
        if identity:
            evidence_ids.extend(ref for ref in identity.get('evidence_refs', []) if store.get('external_evidence', ref))
        evidence_ids.extend(ref for ref in sorted(_nested_refs(fundamental)) if store.get('external_evidence', ref))
        evidence_ids.extend(item['id'] for item in fundamental.get('primary_evidence', [])
            if isinstance(item, dict) and store.get('external_evidence', item.get('id', '')))
        evidence_ids = list(dict.fromkeys(evidence_ids))
        bundle = _operation_evidence_bundle(store, operation_key, evidence_ids, fid)
        evidence_ids = bundle['evidence_ids']
        evidence_order = {ref: index for index, ref in enumerate(evidence_ids)}
        symbol_levels = sorted((l for l in levels if l.get('symbol') == symbol and l.get('verified') is True),
            key=lambda level: evidence_order[level['id']])
        chart_limit_note = None
        try:
            original_frames = _plan_frames(snapshot, symbol_levels)
        except ValueError as error:
            if 'more than eight' not in str(error):
                raise
            chart_limit_note = 'All original chart frames remain archived; this independent valuation does not use the chart price levels. Explicit batch review is required before using them.'
            original_frames, symbol_levels = [], []
        next_review = (parse_time(plan_moment) + timedelta(days=90)).isoformat()
        basis_deadline = _price_basis_deadline(fundamental, plan_moment, set(evidence_ids), store)
        if basis_deadline and parse_time(basis_deadline) < parse_time(next_review):
            next_review = basis_deadline
        policy = {'amount_usd': '1000', 'amount_basis': 'user-authorized default per buy; no cumulative cap',
            'cash_verified': False, 'next_review_at': next_review, 'thesis_valid_from': plan_moment,
            'horizon_until_by_months': {str(months): _horizon_end(plan_moment, months) for months in range(12, 37)},
            'instrument_contract': fundamental['instrument_contract']}
        if chart_limit_note:
            policy['unused_chart_context'] = chart_limit_note
        if identity:
            policy['reviewed_identity'] = identity
            policy['adr_direction'] = 'one ADR represents ordinary_shares_per_adr ordinary shares; never invert this binding'
        current_basis = {}
        execution_accounts = {}
        from .decisions import take_profit_position_basis
        from .ledger import portfolio
        from .reporting import with_reported_holdings
        for account in ('real', 'paper'):
            account_state = with_reported_holdings(store, portfolio(store, account, now=plan_moment))
            execution_accounts[account] = {key: account_state.get(key) for key in
                ('account', 'currency', 'cash', 'cash_complete', 'cash_source', 'cash_status',
                 'holdings_complete', 'cost_complete', 'fees_complete', 'total_equity', 'total_equity_source',
                 'reported_holdings', 'broker_reported_cash', 'broker_reported_net_liquidation',
                 'broker_snapshot_as_of', 'broker_snapshot_recorded_at', 'broker_reconciled')}
            basis = take_profit_position_basis(store, account_state, symbol, now=plan_moment)
            if basis:
                current_basis[account] = basis
        policy.update(cash_verified=execution_accounts['real'].get('cash_complete') is True,
            execution_accounts=execution_accounts, research_does_not_require_execution_cash=True,
            empty_position_basis_means_initial_plan=True, price_basis_valid_until=basis_deadline)
        repair_feedback = None
        if previous and previous.get('status') == 'needs_review' and not context:
            repair_feedback = {'prior_revision': previous['revision'], 'untrusted_prior_review': previous.get('review_issues', []),
                'typed_gaps': previous.get('research_gaps', []), 'instruction': 'Opinions only; independently verify against current original evidence.'}
        if critique:
            repair_feedback = {'source_review_id': critique['source_review_id'], 'prior_revision': critique['source_plan_revision'],
                'untrusted_prior_review': critique['blocking_review_issues'],
                'instruction': 'Critique is opinion, not evidence; verify it against the same original sources.'}
        stored_context = store.get('plan_context', operation_key)
        if stored_context:
            policy, current_basis, repair_feedback = stored_context['policy'], stored_context['current_position_basis'], stored_context['repair_feedback']
        else:
            store.put('plan_context', operation_key, {'policy': policy, 'current_position_basis': current_basis,
                'repair_feedback': repair_feedback}, immutable=True)
        if repair_feedback:
            store.put('research_repair', operation_key, {'source_id': snapshot['id'], 'symbol': symbol,
                'evidence_bundle_hash': bundle['hash'], 'prior_revision': repair_feedback['prior_revision'],
                'status': 'one_attempt_per_verified_evidence_package'}, immutable=True)
        payload = {'instruction': COMPILE, 'symbol': symbol, 'levels': symbol_levels, 'source': snapshot,
            'fundamentals': fundamental, 'evidence_ids': evidence_ids, 'as_of': plan_moment,
            'review_context': context, 'policy': policy, 'current_position_basis': current_basis,
            'evidence_bundle': bundle, 'repair_feedback': repair_feedback}
        if critique:
            payload['instruction'] = critique['compile_instruction']
            payload['critique'] = {key: critique[key] for key in ('id', 'economic_proposal',
                'blocking_review_issues', 'review_is_untrusted_opinion', 'calculation_book',
                'source_plan_hash', 'source_review_hash')}
        if (context or {}).get('kind') == 'system-plan-remediation-v1':
            from .remediation import COMPILE_REPAIR
            payload['instruction'] = COMPILE + COMPILE_REPAIR
            if context.get('repair_policy') in ('system-economic-repair-v1','system-condition-ids-v1'):
                from .remediation import ECONOMIC_REPAIR
                payload['instruction'] += ECONOMIC_REPAIR
            if context.get('repair_policy')=='system-condition-ids-v1':
                from .remediation import CONDITION_IDS
                payload['instruction'] += CONDITION_IDS
        is_etf = fundamental['instrument_contract'].get('asset_type') == 'etf'
        etf_gaps = _etf_input_gaps(fundamental, plan_moment, set(evidence_ids), store) if is_etf else []
        holding_gaps = []
        if holding_source:
            def holding_gap(code, detail):
                holding_gaps.append({'code': code, 'scope': 'research_plan', 'blocking': True,
                    'detail': detail, 'required_evidence': [detail], 'evidence_refs': [fid]})
            if not fundamental.get('financial_report') and not (fundamental.get('primary_financials') or {}).get('facts'):
                holding_gap('HOLDING_FINANCIAL_FACTS_MISSING', '持仓已确认，尚缺可核验的公司财报事实')
            if fundamental['instrument_contract'].get('quote_price_unit') not in {'per_share', 'per_adr'}:
                holding_gap('INSTRUMENT_UNIT_UNVERIFIED', '持仓证券的官方报价与每股盈利单位尚待核实')
            if not model_ready:
                holding_gap('HOLDING_MODEL_UNCONFIGURED', '持仓研究尚未完成独立模型审核')
        preflight_gaps = etf_gaps + holding_gaps
        if preflight_gaps:
            proposed = json.loads(json.dumps(COMPILE_TEMPLATE))['plan']
            proposed.update(currency=fundamental['instrument_contract'].get('quote_currency'),
                price_unit=fundamental['instrument_contract'].get('quote_price_unit'),
                is_adr=False, evidence_refs=[snapshot['id'], fid], targets=[], take_profit=[])
            if holding_source:
                proposed['entry'].update(basis='independent_valuation', conditions=['Required issuer evidence and independent research review must be completed'])
                unit = fundamental['instrument_contract'].get('quote_price_unit')
                proposed['is_adr'] = True if unit == 'per_adr' else False if unit == 'per_share' else None
        elif (context or {}).get('kind') == 'system-plan-remediation-v1' and context.get('review_only_candidate'):
            candidate=context['review_only_candidate']
            if plan_fingerprint(candidate)!=context.get('candidate_hash'):
                raise ValueError('Frozen review-only candidate changed')
            proposed={key:candidate[key] for key in COMPILE_TEMPLATE['plan'] if key in candidate}
            if 'condition_resolution' in candidate:proposed['condition_resolution']=candidate['condition_resolution']
            if 'research_intent' in candidate:proposed['research_intent']=candidate['research_intent']
        elif calculation_seed is not None:
            # Reuse economic inputs, never old reviewer state/verification.
            proposed = {key: calculation_seed[key] for key in COMPILE_TEMPLATE['plan'] if key in calculation_seed}
        else:
            cached_draft = store.get('research_critique_draft', operation_key) if critique else None
            if cached_draft:
                proposed = cached_draft['proposal']
            else:
                try:
                    proposed = _run(model, 'compile_plan', payload, frames=original_frames, now=plan_moment, store=store).get('plan', {})
                except Exception as exc:
                    _record_critique_output_failure(store, operation_key, critique, 'draft', exc)
                    raise
                if critique:
                    store.put('research_critique_draft', operation_key, {'proposal': proposed,
                        'proposal_hash': stable_id(proposed)}, immutable=True)
        if (context or {}).get('repair_policy')=='system-condition-ids-v1' and not context.get('review_only_candidate'):
            from .remediation import expand_condition_ids
            raw_hash=stable_id(proposed)
            proposed=expand_condition_ids(proposed,context,pre_split_deadline=basis_deadline if is_etf else None)
            text=' '.join([str(proposed.get('thesis',''))]+[str(v) for v in proposed.get('entry',{}).get('conditions',[])]).lower()
            if (proposed.get('entry',{}).get('low') is None and proposed.get('entry',{}).get('high') is None
                    and proposed.get('take_profit')==[] and (proposed.get('stop') or {}).get('price') is None
                    and ('research-only' in text or 'no purchase' in text)):
                proposed['research_intent']='no_buy'
            store.put('research_condition_expansion',operation_key,{'raw_proposal_hash':raw_hash,
                'expanded_proposal_hash':stable_id(proposed),'catalog':context['original_condition_catalog']},immutable=True)
        if critique:
            schema_errors = _critique_schema_errors(proposed)
            if schema_errors:
                store.put('research_critique_result', operation_key, {'status': 'schema_invalid',
                    'phase': 'draft', 'issues': schema_errors, 'proposal_hash': stable_id(proposed),
                    'terminal': True, 'semantic_attempts': 1}, immutable=True)
                plans.append(store.get('plan', plan_id) or committed['plan'])
                continue
            current = store.get('plan', plan_id)
            if _critique_superseded(store, operation_key, critique, current):
                plans.append(current)
                continue
        plan, calculation = _calculate_target_prices(proposed)
        for field in ('review_issues', 'review_evidence_id', 'research_gaps', 'research_assessment', 'execution_requirements'):
            plan.pop(field, None)
        calculation['source_operation_id'] = source_operation_key
        if calculation_seed:
            calculation['original_review_evidence_id'] = calculation_seed.get('review_evidence_id')
            calculation['reused_completed_proposal'] = True
        calculation_id = stable_id('plan-calculation', calculation)
        store.put('plan_calculation', calculation_id, dict(calculation, id=calculation_id), immutable=True)
        plan['calculation_provenance'] = dict(calculation, id=calculation_id)
        # Another source can commit while this attempt is waiting for usable
        # output. Keep economic input/as_of frozen, but let the independent
        # reviewer see the actual final revision before sealing any new plan.
        system_repair=(context or {}).get('kind') == 'system-plan-remediation-v1'
        checkpoint_kind='research_system_review_input' if system_repair else 'research_critique_review_input'
        checkpoint = store.get(checkpoint_kind, operation_key) if critique or system_repair else None
        if checkpoint:
            if (stable_id(checkpoint['payload']) != checkpoint['payload_hash']
                    or checkpoint['payload']['evidence_bundle']['hash'] != bundle['hash']):
                raise ValueError('Frozen independent review input changed')
            revision = checkpoint['payload']['plan']['revision']
            assigned = store.get('plan_revision_assignment', stable_id('plan-revision-assignment', operation_key, revision))
            if (not assigned or assigned['operation_id'] != operation_key or assigned['plan_id'] != plan_id
                    or assigned['revision'] != revision or assigned['analysis_as_of'] != plan_moment):
                raise ValueError('Frozen independent review revision assignment missing or changed')
        else:
            current_revision = max([(store.get('plan', plan_id) or {}).get('revision', 0)]+
                [p.get('revision',0) for _,p in store.items('plan_revision') if p.get('id')==plan_id])
            revision = max(attempt['revision'], current_revision + 1)
            assignment_key = stable_id('plan-revision-assignment', operation_key, revision)
            store.put('plan_revision_assignment', assignment_key, {'operation_id': operation_key, 'plan_id': plan_id,
                'attempt_revision': attempt['revision'], 'prior_revision': current_revision, 'revision': revision,
                'analysis_as_of': plan_moment, 'economic_input_hash': stable_id(payload)}, immutable=True)
        plan.update(symbol=symbol, id=plan_id,
            revision=revision, created_at=plan_moment, status='needs_review', source_published_at=published_at)
        if holding_source:
            plan['research_origin'] = 'holding_fundamentals'
            plan['research_preflight'] = holding_gaps
        plan['research_snapshot_id'], plan['fundamental_evidence_id'] = snapshot['id'], fid
        if basis_deadline:
            plan['price_basis_valid_until'] = basis_deadline
        plan['next_review_at'] = next_review
        plan['thesis_valid_from'] = plan_moment
        plan['horizon_until'] = _horizon_end(plan_moment, plan.get('horizon_months'))
        plan['policy'] = {'amount_usd': policy['amount_usd'], 'amount_basis': policy['amount_basis']}
        if isinstance(plan.get('entry'), dict):
            plan['entry']['amount_usd'] = policy['amount_usd']
        if current_basis:
            plan['take_profit_position_basis'] = current_basis
        plan.setdefault('stop', {})
        if isinstance(plan['stop'], dict):
            plan['stop'].setdefault('confirmation', 'daily_close')
        review_payload = {'instruction': REVIEW, 'plan': plan,
            'source': snapshot, 'levels': symbol_levels, 'fundamentals': fundamental,
            'policy': policy, 'current_position_basis': current_basis,
            'evidence_ids': evidence_ids, 'evidence_bundle': bundle, 'repair_feedback': repair_feedback,
            'review_event_id': (context or {}).get('event_id')}
        if (context or {}).get('kind') == 'system-plan-remediation-v1':
            from .remediation import REVIEW_REPAIR
            review_payload['instruction'] = REVIEW + REVIEW_REPAIR
            review_payload['review_context'] = context
            if plan.get('research_intent')=='no_buy':
                review_payload['instruction'] += '\nThis explicitly declares no purchase (research_intent=no_buy). Review the valuation and the no-buy conclusion; do not demand invented entry, stop or take-profit prices for a purchase it does not propose. Return no_buy_reviewed:true only if the no-buy reasoning, sources and scenario math pass. Execution remains prohibited; no-buy approval is not permission to trade.\n'
                if evidence_limited_no_buy(plan):
                    review_payload['instruction'] += '\nvaluation_basis.kind=insufficient_valuation explicitly WITHDRAWS unvalidated price estimates. Review the evidence-backed decision to withhold a purchase because valuation is not established. Do not demand invented EPS/targets or treat the acknowledged evidence gap as a false valuation claim. Accept only if historical facts, disclosed gaps, condition resolutions and the no-buy decision are accurate. Approval confirms the decision to abstain, NOT that a valuation or entry price has been verified.\n'
            if checkpoint:
                review_payload=checkpoint['payload'];plan=json.loads(json.dumps(review_payload['plan']));revision=plan['revision']
            else:
                store.put(checkpoint_kind,operation_key,{'payload':review_payload,'payload_hash':stable_id(review_payload)},immutable=True)
        if critique:
            review_payload['instruction'] = critique['review_instruction']
            review_payload['critique_context'] = {'id': critique['id'], 'prior_issues_are_opinions': True,
                'blocking_review_issues': critique['blocking_review_issues']}
            if checkpoint:
                review_payload = checkpoint['payload']
                plan = json.loads(json.dumps(review_payload['plan']))
                revision = plan['revision']
            else:
                store.put('research_critique_review_input', operation_key,
                    {'payload': review_payload, 'payload_hash': stable_id(review_payload)}, immutable=True)
        if preflight_gaps:
            review = {'accepted': False, 'checks': {}, 'issues': preflight_gaps,
                'deterministic_preflight': True}
        else:
            try:
                review = _run(model, 'review_plan', review_payload, frames=original_frames, now=plan_moment, store=store)
            except Exception as exc:
                _record_critique_output_failure(store, operation_key, critique, 'review', exc)
                raise
        if critique and (not isinstance(review, dict) or
                (review.get('checks') is not None and not isinstance(review['checks'], dict))):
            store.put('research_critique_result', operation_key, {'status': 'schema_invalid',
                'phase': 'review', 'review': review, 'terminal': True, 'semantic_attempts': 1}, immutable=True)
            plans.append(store.get('plan', plan_id) or committed['plan'])
            continue
        if critique:
            store.put('research_critique_review', operation_key,
                {'review': review, 'review_hash': stable_id(review)}, immutable=True)
        try:
            # These are deterministic schema/evidence checks on completed output.
            # Model calls (and their budget/unknown fences) remain outside this guard.
            issues = _assess_plan(plan, review, evidence_ids, fundamental, symbol_levels)
            if (context or {}).get('kind') == 'system-plan-remediation-v1':
                from .remediation import resolution_errors, economic_consistency_errors
                issues.extend(resolution_errors(plan, review, context, set(evidence_ids)))
            if (plan.get('entry') or {}).get('basis') != 'independent_valuation' and not any(l.get('verified') and l.get('symbol') == symbol for l in levels):
                issues.append('No individually verified source levels')
            execution_gaps = classify_plan_validation(plan)['execution_gaps'] if not issues else []
            research_gaps = _typed_research_gaps(issues, review, evidence_ids)
        except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation) as exc:
            if not critique:
                raise
            store.put('research_critique_result', operation_key, {'status': 'schema_invalid',
                'phase': 'assessment', 'error_type': type(exc).__name__,
                'proposal_hash': stable_id(proposed), 'review_hash': stable_id(review),
                'terminal': True, 'semantic_attempts': 1}, immutable=True)
            plans.append(store.get('plan', plan_id) or committed['plan'])
            continue
        plan['status'] = 'active' if not issues and not execution_gaps else 'needs_review'
        plan['review_issues'] = issues
        plan['research_gaps'] = research_gaps
        plan['research_gaps'].extend({'code': 'ENTRY_CONDITION_PENDING', 'scope': 'execution', 'blocking': False,
            'detail': detail, 'required_evidence': ['Resolved entry conditions and price band'], 'evidence_refs': []}
            for detail in execution_gaps)
        plan['research_assessment'] = {'status': 'needs_evidence' if issues else 'approved_conditional' if execution_gaps else 'approved',
            'evidence_bundle_hash': bundle['hash'], 'evidence_generation': fid,
            'repair_status': 'awaiting_new_evidence' if issues else 'reviewed_new_evidence' if repair_feedback else 'initial_review'}
        if not issues and plan.get('research_intent')=='no_buy':
            plan['research_assessment']['status']='approved_no_buy'
        if critique:
            plan['research_assessment'].update(critique_id=critique['id'],
                repair_status='critique_exhausted' if issues else 'critique_accepted')
        plan['execution_requirements'] = ['Fresh official execution quote', 'Reconciled account cash/holdings/fees',
            'Confirmed current holding basis when selling; separate from research approval']
        gap_key = stable_id('research-gap-assessment', plan['id'], revision, bundle['hash'])
        store.put('research_gap', gap_key, {'plan_id': plan['id'], 'revision': revision,
            'evidence_generation': fid, 'gaps': plan['research_gaps'], 'assessment': plan['research_assessment']}, immutable=True)
        if issues:
            plan.pop('thesis_valid_from', None)
            plan.pop('horizon_until', None)
        if issues or review.get('position_basis_accepted') is not True:
            plan.pop('take_profit_position_basis', None)
        plan['research_snapshot_id'] = snapshot['id']
        plan['fundamental_evidence_id'] = fid
        seal_plan_review(store, plan, review, evidence_ids)
        with store.transaction():
            current = store.get('plan', plan_id)
            if _critique_superseded(store, operation_key, critique, current):
                plans.append(current)
                continue
            if (context or {}).get('kind') == 'system-plan-remediation-v1':
                from .remediation import same_published_seed
                if current and not same_published_seed(current,context):
                    plans.append(current)
                    continue
            if (calculation_seed and current and previous
                    and plan_fingerprint(current) != plan_fingerprint(previous)
                    and (current.get('status') == 'active'
                         or current.get('research_assessment', {}).get('status') in ('approved_conditional','approved_no_buy'))):
                plans.append(current)
                continue
            fallback=None
            if issues and system_repair:
                from .remediation import accepted_fallback
                fallback=accepted_fallback(store,context,plan)
            if fallback:
                store.put('plan_revision',stable_id(plan['id'],plan['revision']),plan,immutable=True)
                store.put('plan',plan_id,fallback)
                store.put('research_published_fallback',operation_key,{'failed_revision':plan['revision'],
                    'published_revision':fallback['revision'],'timestamp':moment,'reason':'new_repair_rejected_same_source_evidence'})
            else:
                _persist_plan(store, plan)
            store.put('plan_operation', operation_key, {'plan': plan}, immutable=True)
            if critique:
                store.put('research_critique_result', operation_key, {'status': plan['status'],
                    'plan_id': plan_id, 'revision': revision, 'review_id': plan['review_evidence_id'],
                    'semantic_attempts': 1, 'terminal': True}, immutable=True)
        plans.append(plan)
    return plans


def _stable_fundamentals(value):
    if isinstance(value, dict):
        return {key: _stable_fundamentals(item) for key, item in value.items()
                if key not in {'fetched_at', 'observed_at', 'collected_at', 'reference_quote', 'market_quote',
                               'primary_refresh_after', 'financial_report_request', 'primary_collection'}
                and not (key == 'as_of' and 'financial_report' in value)}
    if isinstance(value, list):
        return [_stable_fundamentals(item) for item in value]
    return value


def ingest_holdings(store, *, model=None, fundamentals=None, now=None, seed_only=False):
    """Seed missing real holding research without pretending it came from video.

    A stable symbol source anchors logical call fences. Facts remain separately
    sealed in fundamental_evidence; quotes, quantity guesses and receipt clocks
    cannot create a fresh paid operation for unchanged issuer evidence.
    """
    from .ledger import portfolio
    from .reporting import with_reported_holdings
    moment = now or utc_now()
    state = with_reported_holdings(store, portfolio(store, 'real', now=moment))
    holdings = {}
    for row in state.get('reported_holdings') or []:
        try:
            value = decimal_value(row.get('reported_market_value', row.get('market_value')))
            if value > 0:
                holdings[symbol_value(row['symbol'])] = {'kind': 'reported_holding',
                    'broker_snapshot_id': state.get('broker_snapshot_id')}
        except (ValueError, KeyError, TypeError):
            continue
    for row in state.get('positions') or []:
        try:
            if decimal_value(row.get('quantity')) > 0:
                holdings[symbol_value(row['symbol'])] = {'kind': 'confirmed_ledger_position'}
        except (ValueError, KeyError, TypeError):
            continue
    result = {'symbols': sorted(holdings), 'plans': [], 'errors': [], 'skipped_existing': [],
        'deferred_unresolved_risk': []}
    existing = {plan.get('symbol'): plan for _, plan in store.items('plan')}
    for symbol, origin in sorted(holdings.items()):
        prior = existing.get(symbol)
        source = store.get('research_evidence', (prior or {}).get('research_snapshot_id', '')) or {}
        if prior and source.get('source_type') != 'holding_fundamentals':
            result['skipped_existing'].append(symbol)
            continue
        if prior and seed_only:
            result['plans'].append(prior)
            continue
        binding = store.get('research_plan_review', (prior or {}).get('review_evidence_id', '')) or {}
        if prior and model is None and not (binding.get('review') or {}).get('deterministic_preflight'):
            result['plans'].append(prior)
            continue  # The quote loop must not overwrite a reviewed plan with a placeholder.
        sid = stable_id('holding-research-source-v1', symbol)
        with store.transaction():
            source = store.get('research_evidence', sid)
            if not source:
                source = {'id': sid, 'source_type': 'holding_fundamentals', 'symbol': symbol,
                    'title': symbol + ' existing holding: independent fundamental research',
                    'first_observed_at': moment, 'origin': dict(origin, account='real', symbol=symbol),
                    'scope': 'Confirmed or reported existing holding. No video, acquisition price, quantity estimate or completed research is implied.'}
                store.put('research_evidence', sid, source, immutable=True)
        data = (fundamentals or {}).get(symbol)
        if data is None:
            data = (store.get('fundamental_cache', symbol) or {}).get('data') or {}
        try:
            if prior is None and model is not None:
                # Ensure a visible, non-actionable research record exists even
                # if the first paid request is budget-deferred or unresolved.
                compile_plan(store, source, [], None, {symbol: data}, moment)
            compiled = compile_plan(store, source, [], model, {symbol: data}, moment,
                deferred=result['deferred_unresolved_risk'])
            result['plans'].extend(store.get('plan', plan['id']) or plan for plan in compiled)
            current = store.get('plan', stable_id('investment-plan', symbol))
            store.put('holding_research_state', symbol, {'symbol': symbol, 'source_id': sid,
                'status': current.get('research_assessment', {}).get('status') if current else 'deferred',
                'error': None, 'timestamp': moment})
        except Exception as exc:
            result['errors'].append({'symbol': symbol, 'error': type(exc).__name__,
                'detail': 'Holding research deferred; inspect persisted evidence and model receipts'})
            current = store.get('plan', stable_id('investment-plan', symbol))
            if current:
                result['plans'].append(current)
            store.put('holding_research_state', symbol, {'symbol': symbol, 'source_id': sid,
                'status': 'deferred', 'error': type(exc).__name__, 'timestamp': moment})
    return result


def ingest_archives(store, archive_db, *, model=None, symbols=None, fundamentals=None, now=None, research_since=None,
                    defer_missing_fundamentals=False):
    moment = now or utc_now()
    cutoff = parse_time(research_since) if research_since is not None else None
    result = {'ingested': 0, 'unchanged': 0, 'errors': [], 'plans': [], 'deferred_before_since': [],
              'research_since': cutoff.isoformat() if cutoff else None, 'historical_revisions': [],
              'deferred_unresolved_risk': [], 'deferred_missing_fundamentals': [], 'held': []}
    with contextlib.closing(sqlite3.connect(Path(archive_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT video_id,revision,source,title,payload,analysis_sha256 FROM analyses ORDER BY video_id,revision').fetchall()
    latest = _latest_revisions(rows)
    for row in sorted(rows, key=_row_order):
        seal_validated = False
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
            seal_validated = True
            if row[1] != latest[row[0]]:
                result['historical_revisions'].append({'video_id': row[0], 'revision': row[1],
                    'latest_revision': latest[row[0]], 'evidence_id': snapshot['id']})
                if latest[row[0]] is None:
                    _block_old_revision_plans(store, row[0])
                continue
            if cutoff is not None and parse_time(_published_at(snapshot['metadata'])) < cutoff:
                result['deferred_before_since'].append({'video_id': snapshot['video_id'],
                    'revision': snapshot['revision'], 'published_at': _published_at(snapshot['metadata']),
                    'evidence_id': snapshot['id']})
                continue
            identities = {key: value['id'] for key, value in store.items('instrument_identity')}
            supplements = {key: value['id'] for key, value in store.items('research_supplement_current')}
            workflow_id = stable_id(snapshot['id'], 'workflow-v6-calculator-v1', sorted(symbols or []),
                _stable_fundamentals(fundamentals or {}), identities, supplements, _model_cache_identity(model))
            completed = store.get('research_workflow', workflow_id)
            if model is None or completed:
                if completed:
                    for cached_plan in completed['plans']:
                        current = store.get('plan', cached_plan['id'])
                        risk_defer = _unresolved_thesis_defer(store, current, snapshot, fundamentals, moment)
                        if risk_defer:
                            result['deferred_unresolved_risk'].append(risk_defer)
                        else:
                            candidate = cached_plan
                            if model is not None and cached_plan.get('status') == 'needs_review':
                                cached_levels = [level for _, level in store.items('research_level')
                                    if snapshot['id'] in level.get('evidence_refs', [])
                                    and level.get('symbol') == cached_plan['symbol']]
                                revised = compile_plan(store, snapshot, cached_levels, model, fundamentals, moment)
                                candidate = next((plan for plan in revised if plan['symbol'] == cached_plan['symbol']), candidate)
                            result['plans'].append(candidate)
                    result['deferred_unresolved_risk'].extend(completed.get('deferred_unresolved_risk', []))
                continue
            attempt = store.get('research_attempt', workflow_id)
            if not attempt:
                attempt = {'timestamp': moment}
                store.put('research_attempt', workflow_id, attempt, immutable=True)
            workflow_moment = attempt['timestamp']
            extraction_key = stable_id('source-extraction-v3-primary-transport', snapshot['id'], _model_cache_identity(model))
            extraction = store.get('research_extraction', extraction_key)
            if extraction:
                levels = extraction['levels']
            else:
                levels = []
                layout=_extraction_layout(store,snapshot)
                selected_frames=[f for f in snapshot['frames'] if f['timestamp_seconds'] in layout['frame_seconds']]
                frames = _frames(dict(snapshot,frames=selected_frames))
                instruction=EXTRACT
                if layout['policy']=='accepted_claim_frames':
                    instruction += ('\nOnly extract the principal instruments substantively discussed in the full transcript. '
                        'Ignore sidebar quote tickers and names merely mentioned in passing. '
                        'The provided frames are the accepted report scope; values must still be checked from original evidence.')
                for start in range(0, len(frames), 8):
                    batch = frames[start:start + 8]
                    batch_source = dict(snapshot, frames=selected_frames[start:start + 8])
                    request={'instruction':instruction,'source':batch_source,
                             'provided_frame_seconds':[s for s,_ in batch]}
                    if layout['policy']=='accepted_claim_frames':
                        request['topic_scope']=[c.get('headline') for c in snapshot.get('analysis',{}).get('claims',[])]
                    extracted = _run(model, 'extract_levels', request, batch, workflow_moment)
                    checked = _run(model, 'verify_levels', {'instruction': VERIFY, 'levels': extracted,
                        'source': batch_source}, batch, workflow_moment)
                    levels.extend(_verify_levels(batch_source, extracted, checked))
                levels = list({l['id']: l for l in sorted(levels, key=lambda item: bool(item.get('verified')))}.values())
                store.put('research_extraction', extraction_key, {'levels': levels}, immutable=True)
            for level in levels:
                store.put('research_level', level['id'], level, immutable=True)
                layout=(store.get('research_extraction_layout',stable_id('research-extraction-layout-v2',snapshot['id']))
                    or store.get('research_extraction_layout',stable_id('research-extraction-layout-v1',snapshot['id'])) or {})
                if (layout.get('policy')=='accepted_claim_frames'
                        and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}',str(level.get('symbol','')))):
                    store.put('research_discovered_symbol',level['symbol'],{'symbol':level['symbol'],
                        'video_id':snapshot['video_id'],'source_id':snapshot['id'],
                        'purpose':'fetch_independent_fundamentals_only','approval':False})
            if symbols:
                levels = [l for l in levels if l.get('symbol') in symbols]
            if defer_missing_fundamentals:
                missing=sorted({l['symbol'] for l in levels if l.get('symbol') and l['symbol'] not in (fundamentals or {})})
                result['deferred_missing_fundamentals'].extend(missing)
                # Quotes/fundamentals discover these persisted symbols on the next
                # regular tick. Do not pay to compile a deliberately empty bundle.
                levels=[l for l in levels if l.get('symbol') not in missing]
            source_deferred = []
            plans = compile_plan(store, snapshot, levels, model, fundamentals, workflow_moment,
                operation_id=workflow_id, deferred=source_deferred)
            store.put('research_workflow', workflow_id, {'id': workflow_id, 'plans': plans,
                'level_ids': [l['id'] for l in levels], 'completed_at': workflow_moment,
                'deferred_unresolved_risk': source_deferred}, immutable=True)
            result['plans'].extend(plans)
            result['deferred_unresolved_risk'].extend(source_deferred)
        except Exception as exc:
            if not seal_validated and row[1] == latest.get(row[0]):
                _block_old_revision_plans(store, row[0])
            from .model import ModelOutputInvalid
            if defer_missing_fundamentals and isinstance(exc,ModelOutputInvalid) and store.get('model_receipt',getattr(exc,'call_id','')):
                hold={'video_id':row[0],'revision':row[1],'error':'ModelOutputInvalid',
                      'call_id':exc.call_id,'status':'needs_review','paid_replay_allowed':False}
                store.put('research_archive_hold',stable_id(row[0],row[1]),hold)
                result['held'].append(hold)
                continue
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
    financial_event = (isinstance(evidence, dict)
        and ('financial_report' in evidence or 'fundamentals' in evidence))
    attempt = store.get('review_attempt', key)
    if not attempt:
        prior_fundamental = store.get('fundamental_evidence', plan.get('fundamental_evidence_id', '')) or {}
        latest = store.get('fundamental_cache', plan['symbol']) or {}
        if financial_event and latest.get('data'):
            data = {plan['symbol']: latest['data']}
        elif (evidence or {}).get('fundamentals'):
            data = evidence['fundamentals']
        elif isinstance(evidence, dict) and 'financial_report' in evidence:
            data = {plan['symbol']: evidence}
        else:
            data = {plan['symbol']: latest.get('data') or prior_fundamental.get('data', {})}
        attempt = {'timestamp': moment, 'fundamentals': data, 'evidence': evidence}
        store.put('review_attempt', key, attempt, immutable=True)
    moment, evidence, data = attempt['timestamp'], attempt['evidence'], attempt['fundamentals']
    already_reviewed = False
    current_context = {}
    if financial_event:
        # An old queued trigger/attempt is historical evidence, not permission
        # to replace a newer independently reviewed issuer package. Information
        # receipt order selects the current cache; original dates remain intact.
        latest = (store.get('fundamental_cache', plan['symbol']) or {}).get('data')
        bound = (store.get('fundamental_evidence', plan.get('fundamental_evidence_id', '')) or {}).get('data')
        selected = latest or data.get(plan['symbol']) or bound or {}
        data = {plan['symbol']: selected}
        current, _ = _research_fundamental(store, plan['symbol'], selected)
        fid = stable_id('fundamentals', _stable_fundamentals(current))
        if not store.get('fundamental_evidence', fid):
            store.put('fundamental_evidence', fid, {'id': fid, 'data': current,
                'first_observed_at': now or utc_now()}, immutable=True)
        same_facts = fid == plan.get('fundamental_evidence_id')
        binding = store.get('research_plan_review', plan.get('review_evidence_id', '')) or {}
        already_reviewed = same_facts and binding.get('plan_hash') == plan_fingerprint(plan)
        original = (evidence.get('fundamentals') or {}).get(plan['symbol']) if evidence.get('fundamentals') else evidence
        original, _ = _research_fundamental(store, plan['symbol'], original)
        current_refs = [fid] + sorted(ref for ref in _nested_refs(current) if store.get('external_evidence', ref))
        current_context = {'current_fundamentals': current,
            'current_evidence_bundle': _evidence_bundle(store, current_refs, fid),
            'original_trigger_is_current': _logical_fundamentals(original) == _logical_fundamentals(current),
            'instruction': 'Original trigger is historical audit material. Evaluate risk using current dated primary facts; never restore older fundamentals or treat a prior review opinion as fact.'}
        from .ledger import portfolio
        from .reporting import reported_holdings
        held = any(item.get('symbol') == plan['symbol'] for item in reported_holdings(store, now=moment))
        for account in ('real', 'paper'):
            state = portfolio(store, account, now=moment)
            positions = (state.get('positions') or []) + (state.get('reported_holdings') or [])
            held = held or any(item.get('symbol') == plan['symbol'] for item in positions)
        pending_holding = (plan.get('research_origin') == 'holding_fundamentals'
            and (binding.get('review') or {}).get('deterministic_preflight') is True)
        if already_reviewed and (not held or pending_holding):
            record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'], 'event_id': event_id,
                'review': {'accepted': binding.get('accepted') is True, 'issues': plan.get('review_issues', [])},
                'revised_plan': None, 'status': plan['status'], 'deduplicated': True,
                'original_trigger_preserved_in': key, 'current_fundamental_evidence_id': fid,
                'reason': 'ALREADY_INDEPENDENTLY_REVIEWED_SAME_EVIDENCE'}
            store.put('plan_review', key, record, immutable=True)
            return record
    raw = {'event_id': event_id, 'symbol': plan['symbol'], 'timestamp': moment, 'raw_evidence': evidence}
    if current_context:
        # Bind the actual current facts seen by both risk reviewers, as well as
        # the historical trigger; raw_id must not refer to the old report alone.
        raw['current_research_context'] = current_context
    raw_id = stable_id('review-event-evidence', raw)
    store.put('review_event_evidence', raw_id, dict(raw, id=raw_id), immutable=True)
    snapshot = store.get('research_evidence', plan.get('research_snapshot_id', ''))
    risk_defer = _unresolved_thesis_defer(store, plan,
        snapshot or {'id': plan.get('research_snapshot_id')}, data, moment)
    if risk_defer:
        record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'], 'event_id': event_id,
            'review': {'accepted': False, 'issues': [risk_defer['code']]}, 'revised_plan': None,
            'status': 'deferred', 'deferred_unresolved_risk': [risk_defer]}
        store.put('plan_review', key, record, immutable=True)
        return record
    result = _run(model, 'event_review', {'instruction': EVENT_REVIEW, 'plan': plan,
        'event_id': event_id, 'new_evidence': evidence, 'new_evidence_refs': [raw_id],
        'current_research_context': current_context}, now=moment)
    assessment = result.get('assessment') or {}
    if assessment.get('triggered') is True:
        independent = _run(model, 'verify_invalidation', {'instruction': VERIFY_INVALIDATION,
            'plan': plan, 'event_id': event_id, 'assessment': assessment,
            'new_evidence': evidence, 'new_evidence_refs': [raw_id],
            'current_research_context': current_context}, now=moment)
        if independent.get('accepted') is True:
            from .decisions import record_thesis_risk
            risk = record_thesis_risk(store, plan, assessment, independent, now=moment)
            with store.transaction():
                current = store.get('plan', plan['id'])
                if current and plan_fingerprint(current) == plan_fingerprint(plan):
                    store.put('plan', plan['id'], dict(current, status='invalidated'))
                    for namespace, statuses in [('advice_intent', {'pending', 'suspended'}), ('reservation', {'active'})]:
                        for intent_key, intent in store.items(namespace):
                            if intent.get('plan_id') == plan['id'] and intent.get('status') in statuses:
                                store.put(namespace, intent_key, dict(intent,
                                    status='cancelled' if namespace == 'advice_intent' else 'released',
                                    reason='verified_thesis_invalidation'))
                record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'], 'event_id': event_id,
                    'review': result, 'risk_evidence_id': risk['id'], 'revised_plan': None, 'status': 'invalidated'}
                store.put('plan_review', key, record, immutable=True)
            return record
    if financial_event and already_reviewed:
        record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'], 'event_id': event_id,
            'review': result, 'revised_plan': None, 'status': plan['status'], 'risk_review_only': True,
            'deduplicated': True, 'current_fundamental_evidence_id': fid,
            'reason': 'ALREADY_INDEPENDENTLY_REVIEWED_SAME_EVIDENCE'}
        store.put('plan_review', key, record, immutable=True)
        return record
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
        result = dict(result, accepted=False, issues=_review_issues(result.get('issues')) + ['Original sealed research snapshot unavailable'])
    record = {'id': key, 'timestamp': moment, 'plan_id': plan['id'],
        'event_id': event_id, 'review': result, 'revised_plan': revised,
        'status': revised['status'] if revised else 'needs_review'}
    store.put('plan_review', key, record, immutable=True)
    return record
