"""Reuse completed research on identical primary facts, including rejected drafts."""
import json
from .common import stable_id


def completed_evidence_plan(store, snapshot, symbol, fundamental, context, now):
    from .research import _logical_fundamentals
    # New external risk evidence needs its own assessment. Internal repair IDs,
    # reviewer opinions and revision counters are not new issuer/source facts.
    if context and context.get('kind') != 'system-plan-remediation-v1' and context.get('new_evidence'):
        return None
    facts = _logical_fundamentals(fundamental)
    candidates = []
    for _, operation in store.items('plan_operation'):
        plan = operation.get('plan') or {}
        if plan.get('symbol') != symbol or plan.get('research_snapshot_id') != snapshot['id']:
            continue
        record = store.get('fundamental_evidence', plan.get('fundamental_evidence_id', ''))
        if record and _logical_fundamentals(record.get('data')) == facts:
            candidates.append(plan)
    if not candidates:
        return None
    result = max(candidates, key=lambda plan: plan.get('revision', 0))
    # Do not resurrect a superseded result over a newer current plan.
    current = store.get('plan', result['id'])
    if current and current.get('revision', 0) > result.get('revision', 0):
        bound = store.get('fundamental_evidence', current.get('fundamental_evidence_id', ''))
        if (current.get('research_snapshot_id') != snapshot['id'] or not bound
                or _logical_fundamentals(bound.get('data')) != facts):
            return None
        result = current
    key = stable_id('research-evidence-reuse-v1', snapshot['id'], symbol, facts)
    store.put('research_evidence_reuse', key, {'timestamp': now, 'symbol': symbol,
        'source_id': snapshot['id'], 'plan_id': result['id'], 'revision': result['revision'],
        'status': result['status'], 'reason': 'same_primary_evidence_no_new_paid_revision',
        'new_paid_calls': 0})
    # Reuse is never approval. Keep rejection, conditions and evidence bindings intact.
    return json.loads(json.dumps(result))
