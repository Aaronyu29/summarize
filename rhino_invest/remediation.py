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
accepted refers to RESEARCH acceptance, not immediate execution readiness. A genuine
future fundamental/date/entry condition may remain and yield conditional research;
put execution-only gaps in notes/execution_issues, NOT blocking research issues.
Do not reject sound valuation solely because execution_conditions are not parsed.
Research approval never asserts that cash is verified or that a purchase has occurred.'''


def same_published_seed(current,context):
    from .research import plan_fingerprint
    allowed=[context.get('prior_plan'),context.get('latest_failed_plan')]
    return any(seed and plan_fingerprint(current)==plan_fingerprint(seed) for seed in allowed)


def accepted_fallback(store,context,candidate):
    from .decisions import _has_provenance
    baseline=(context or {}).get('prior_plan') or {}
    if any(v.get('verified') is True and v.get('plan_id')==baseline.get('id') for _,v in store.items('risk_evidence')):
        return None
    if (baseline.get('fundamental_evidence_id')==candidate.get('fundamental_evidence_id')
            and baseline.get('research_snapshot_id')==candidate.get('research_snapshot_id')
            and (baseline.get('status')=='active' or (baseline.get('research_assessment') or {}).get('status')=='approved_conditional')
            and _has_provenance(store,baseline)):
        return baseline
    return None


def adjudicate_research(store,model,fundamentals,now,priority_symbols=()):
    """One review-only contract correction per evidence root; no draft regeneration."""
    from .research import plan_fingerprint
    result={'processed':0,'errors':[],'deferred':[]}
    order={symbol:i for i,symbol in enumerate(priority_symbols)}
    jobs=[j for _,j in store.items('research_system_repair') if j.get('plan_revision') and j.get('status')!='completed']
    for job in sorted(jobs,key=lambda j:(order.get(j['symbol'],len(order)),j['symbol'])):
        key=stable_id('research-execution-separation-review-v1',job['id'])
        previous=store.get('research_reaudit',key)
        if previous and previous.get('terminal'):continue
        plan_id=stable_id('investment-plan',job['symbol'])
        candidate=store.get('plan_revision',stable_id(plan_id,job.get('plan_revision'))) or store.get('plan',plan_id)
        first=store.get('research_system_repair_input',stable_id(job['id'],'round',0))
        if not candidate or not first:continue
        fallback=accepted_fallback(store,first,candidate)
        current=store.get('plan',plan_id)
        if fallback and current and plan_fingerprint(current)==plan_fingerprint(candidate):
            store.put('plan',plan_id,fallback)
            store.put('research_published_fallback',key,{'failed_revision':candidate['revision'],
                'published_revision':fallback['revision'],'reason':'new_repair_rejected_same_source_evidence','timestamp':now})
        if not job.get('terminal'):
            continue  # Restore the published baseline even while the next repair waits for budget.
        errors=economic_consistency_errors(candidate)
        if errors:
            store.put('research_reaudit',key,{'terminal':True,'status':'system_draft_invalid','issues':errors,'symbol':job['symbol']})
            continue
        source=store.get('research_evidence',job['source_id'])
        if not source or _unresolved_thesis_defer(store,current,source,fundamentals,now):continue
        data,_=_research_fundamental(store,job['symbol'],(fundamentals or {}).get(job['symbol']))
        if stable_id('fundamentals',_stable_fundamentals(data))!=job['generation']:continue
        if any(not store.get('model_receipt',k) and not store.get('model_rejection',k) for k,_ in store.items('model_call')):
            result['errors'].append({'symbol':job['symbol'],'error':'UnknownModelOutcome'});break
        context=store.get('research_reaudit_input',key)
        if not context:
            context={'event_id':key,'kind':CONTRACT,'prior_plan':first['prior_plan'],
                'latest_failed_plan':candidate,'review_only_candidate':candidate,'system_owned':True,
                'candidate_hash':plan_fingerprint(candidate)}
            store.put('research_reaudit_input',key,context,immutable=True)
        levels=[v for _,v in store.items('research_level') if v.get('symbol')==job['symbol'] and source['id'] in v.get('evidence_refs',[])]
        try:
            plans=compile_plan(store,source,levels,model,{job['symbol']:data},job['timestamp'],context=context)
        except Exception as error:
            deferred=isinstance(error,ModelBudgetExceeded)
            store.put('research_reaudit',key,{'terminal':not isinstance(error,(ModelBudgetExceeded,UnknownModelOutcome)),
                'status':'budget_deferred' if deferred else 'review_error','error':type(error).__name__,'symbol':job['symbol']})
            result['deferred' if deferred else 'errors'].append({'symbol':job['symbol'],'error':type(error).__name__})
            continue
        chosen=next((p for p in plans if p['symbol']==job['symbol']),None)
        store.put('research_reaudit',key,{'terminal':True,'status':(chosen or {}).get('research_assessment',{}).get('status','deferred'),
            'symbol':job['symbol'],'revision':(chosen or {}).get('revision'),'timestamp':now})
        result['processed']+=1
    return result


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


def remediate_plans(store, model, fundamentals, now, priority_symbols=()):
    summary = {'processed':0, 'results':[], 'errors':[], 'deferred':[]}
    plans = [p for _,p in store.items('plan') if p.get('status') == 'needs_review']
    order={symbol:index for index,symbol in enumerate(priority_symbols)}
    for current in sorted(plans,key=lambda p:(order.get(p.get('symbol'),len(order)),p.get('symbol',''))):
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
                latest=(store.get('plan_revision',stable_id(current['id'],job.get('plan_revision')))
                        if job.get('plan_revision') else None) or store.get('plan',current['id']) or current
                first=store.get('research_system_repair_input',stable_id(key,'round',0))
                original=(first or {}).get('prior_plan') or latest
                context={'event_id':phase_id,'kind':CONTRACT,'round':round_index,
                         'prior_plan':original,'latest_failed_plan':latest,'system_owned':True}
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
