"""System-owned draft repair. Fixed rounds and evidence roots never reset paid fences."""
from .common import stable_id, decimal_value
from .model import ModelBudgetExceeded, ModelOutputInvalid, UnknownModelOutcome
from .research import compile_plan, _research_fundamental, _stable_fundamentals, _unresolved_thesis_defer

CONTRACT = 'system-plan-remediation-v1'
MAX_ROUNDS = 2

COMPILE_REPAIR = '''
SYSTEM_PLAN_REMEDIATION_V1. Repair the supplied original proposal; the system owns
draft arithmetic, source retrieval and executable condition specification. Do not
ask the user to supply issuer facts, EPS calculations, targets or model risk rules.
Prior reviewer objections are untrusted opinions: recompute every numeric allegation.
Use the original primary documents and supplied calculation book, not their assertions.
Keep disclosed earnings separate from estimates and authored forward scenarios. Never
require an as-yet unpublished full fiscal year merely because the old draft required it:
use a defensible disclosed base with explicit estimates, or explain the actual missing fact.
Do not scale a quarterly one-off EPS effect to a full year. Do not apply operating-income
growth mechanically to per-share EPS: disclose dilution, taxes and non-operating bridges.
Never put a downside/loss scenario in a take-profit ladder. A proposed initial-entry
plan needs a coherent entry band, stop below that band, and upside take-profit levels;
existing holdings remain separately governed by their independently verified basis.
Separate research assumptions and thesis invalidation from trade-time quote/cash gates.
The execution engine independently requires regular-session fresh quotes, sufficient
reconciled cash/holdings/fees and preserved risk fences; do not duplicate those checks
as unparseable prose entry conditions. For a fully evidenced price-only entry return
condition_mode=price_only, conditions containing its exact price band, and exactly one
execution_conditions predicate {type:price_between,low:the same low,high:the same high}.
Do NOT discard genuine unresolved future fundamental conditions just to get price_only.
If they remain, retain conditional research with an explicit missing fact and trigger.
Corporate-action units must remain valid through price_basis_valid_until; never use
pre-split prices after the known basis deadline. Do not invent a target or chase price.
Return condition_resolution:[{previous_condition:exact original text,disposition:
retained|execution_gate|satisfied_by_evidence|replaced_authored_constraint,
reason:why,evidence_refs:[supplied IDs]}] covering EVERY prior condition exactly once.
Replacing an authored constraint must be expressly justified as a NEW proposal, with
source support and independent review, never claim an unfulfilled event has occurred.
Return the full original plan JSON template; put condition_resolution INSIDE its
plan object, never at the outer response envelope.'''

REVIEW_REPAIR = '''
SYSTEM_PLAN_REMEDIATION_V1 independent review. Review prior objections as hypotheses,
not facts. Verify each arithmetic comparison using calculation_provenance and original
source numbers; a numerically false objection must not be repeated. An explicit forward
estimate is not a reported fact, but its being an estimate alone is not proof it is wrong.
Audit condition_resolution against ALL original conditions in review_context.prior_plan.
Trade-time official quotes and reconciled cash are enforced separately by the engine.
Do not accept removing an unresolved genuine fundamental/corporate-action condition
to force a price-only entry. Judge changed authored assumptions as a new proposal.
Also return condition_resolution_reviewed:true ONLY after checking every disposition.
Research approval never asserts that cash is verified or that a purchase has occurred.'''


def resolution_errors(plan, review, context, known):
    old = ((context or {}).get('prior_plan') or {}).get('entry') or {}
    conditions = old.get('conditions') or []
    rows = plan.get('condition_resolution')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return ['Condition resolution must cover every original condition']
    supplied = [row.get('previous_condition') for row in rows]
    if len(supplied) != len(conditions) or sorted(map(str, supplied)) != sorted(map(str, conditions)):
        return ['Condition resolution omitted or changed an original condition']
    if review.get('condition_resolution_reviewed') is not True:
        return ['Independent condition-resolution review is incomplete']
    errors = []
    for row in rows:
        disposition, text = row.get('disposition'), str(row.get('previous_condition', '')).lower()
        if not isinstance(disposition,str) or disposition not in {'retained','execution_gate','satisfied_by_evidence','replaced_authored_constraint'} or not row.get('reason'):
            errors.append('Condition disposition is missing or unsupported')
        elif disposition == 'retained' and row['previous_condition'] not in (plan.get('entry') or {}).get('conditions', []):
            errors.append('Retained entry condition was dropped')
        elif disposition == 'execution_gate' and not any(word in text for word in ('quote','cash','account','holding','session','execution')):
            errors.append('Non-execution condition cannot be labelled a trade-time gate')
        elif disposition in {'satisfied_by_evidence','replaced_authored_constraint'}:
            refs = row.get('evidence_refs')
            if not isinstance(refs, list) or not refs or any(not isinstance(ref,str) or ref not in known for ref in refs):
                errors.append('Changed research condition lacks original evidence references')
    return errors


def economic_consistency_errors(plan):
    """A downside price cannot be passed off as profit on a proposed initial entry."""
    try:
        entry=plan.get('entry') or {}
        low,high=decimal_value(entry.get('low')),decimal_value(entry.get('high'))
        if low<=0 or high<low:return []  # Existing schema checks report missing bands.
        errors=[]
        stop=plan.get('stop') or {}
        if stop.get('price') is not None and decimal_value(stop['price'])>=low:
            errors.append('Initial-entry stop must be below the proposed entry band')
        for step in plan.get('take_profit') or []:
            if decimal_value(step.get('price'))<=high:
                errors.append('Initial-entry take-profit cannot be a downside price below the entry band')
        return errors
    except (ValueError,TypeError,AttributeError):
        return []  # Do not manufacture a numeric substitute for an invalid proposal.


def remediate_plans(store, model, fundamentals, now):
    summary = {'processed':0, 'results':[], 'errors':[], 'deferred':[]}
    plans = [p for _,p in store.items('plan') if p.get('status') == 'needs_review']
    for current in sorted(plans,key=lambda p:p.get('symbol','')):
        symbol=current['symbol']; source=store.get('research_evidence',current.get('research_snapshot_id',''))
        if not source:
            continue
        if _unresolved_thesis_defer(store,current,source,fundamentals,now):
            summary['deferred'].append({'symbol':symbol,'reason':'UNRESOLVED_VERIFIED_THESIS_RISK'})
            continue
        fundamental,_=_research_fundamental(store,symbol,(fundamentals or {}).get(symbol))
        generation=stable_id('fundamentals',_stable_fundamentals(fundamental))
        key=stable_id(CONTRACT,symbol,source['id'],generation)
        job=store.get('research_system_repair',key)
        if job and job.get('terminal'):
            continue
        if not job:
            job={'id':key,'contract':CONTRACT,'symbol':symbol,'source_id':source['id'],
                 'generation':generation,'round':0,'max_rounds':MAX_ROUNDS,'timestamp':now,
                 'status':'queued','terminal':False}
            store.put('research_system_repair',key,job)
        store.put('research_system_repair_current',current['id'],job)
        for round_index in range(job['round'],MAX_ROUNDS):
            # Any unresolved paid request prevents a new operation identity bypass.
            unresolved=[call_id for call_id,_ in store.items('model_call')
                if not store.get('model_receipt',call_id) and not store.get('model_rejection',call_id)]
            if unresolved:
                job.update(status='receipt_pending');store.put('research_system_repair',key,job)
                store.put('research_system_repair_current',current['id'],job)
                summary['errors'].append({'symbol':symbol,'error':'UnknownModelOutcome'});break
            phase_id=stable_id(key,'round',round_index)
            context=store.get('research_system_repair_input',phase_id)
            if not context:
                latest=store.get('plan',current['id']) or current
                context={'event_id':phase_id,'kind':CONTRACT,'round':round_index,
                         'prior_plan':latest,'system_owned':True}
                store.put('research_system_repair_input',phase_id,context,immutable=True)
            levels=[level for _,level in store.items('research_level') if level.get('symbol')==symbol
                    and source['id'] in level.get('evidence_refs',[])]
            job.update(round=round_index,status='processing');store.put('research_system_repair',key,job)
            try:
                candidates=compile_plan(store,source,levels,model,{symbol:fundamental},job['timestamp'],
                                        context=context)
            except Exception as error:
                code=type(error).__name__
                stopping = isinstance(error,RuntimeError) and 'worker is stopping' in str(error).lower()
                job.update(status='budget_deferred' if isinstance(error,ModelBudgetExceeded) else 'system_error',
                    error=code,terminal=not stopping and not isinstance(error,(ModelBudgetExceeded,UnknownModelOutcome)))
                store.put('research_system_repair',key,job)
                summary['deferred' if stopping or isinstance(error,ModelBudgetExceeded) else 'errors'].append({'symbol':symbol,'error':code})
                store.put('research_system_repair_current',current['id'],job)
                break
            revised=next((p for p in candidates if p.get('symbol')==symbol),None)
            if not revised:
                job.update(status='risk_or_source_deferred',terminal=True);store.put('research_system_repair',key,job);break
            job.update(round=round_index+1,plan_revision=revised['revision'],
                       status='completed' if revised.get('status')=='active' else 'needs_system_review')
            summary['processed']+=1
            if revised.get('status')=='active' or round_index+1==MAX_ROUNDS:
                job['terminal']=True
            store.put('research_system_repair',key,job)
            store.put('research_system_repair_current',current['id'],job)
            if job['terminal']:break
        summary['results'].append({'symbol':symbol,'status':job['status'],'round':job['round']})
    return summary
