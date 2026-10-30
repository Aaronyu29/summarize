"""System-owned draft repair. Fixed rounds and evidence roots never reset paid fences."""
from .common import stable_id, decimal_value
from .model import ModelBudgetExceeded, ModelOutputInvalid, UnknownModelOutcome
from .research import compile_plan, _research_fundamental, _stable_fundamentals, _unresolved_thesis_defer

CONTRACT = 'system-plan-remediation-v1'
MAX_ROUNDS = 2
ECONOMIC_REPAIR_CONTRACT = 'system-economic-repair-v1'
CONDITION_REPAIR_CONTRACT = 'system-condition-ids-v1'

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

ECONOMIC_REPAIR = '''
ECONOMIC_REPAIR_V1: The earlier completed proposal has a mechanically verified
entry/stop/profit contradiction. Author a coherent replacement, not cosmetic prose.
Use calculation_feedback as arithmetic evidence, not authority for its old assumptions.
Do not copy the old entry band when its base target is inside/below that band. An
initial buy must have a justified lower entry and upside exits; if no purchase is
supported, explicitly return research-only with missing entry/targets, not a fake buy.
Every take-profit step must be ABOVE entry.high; a bear target is a risk scenario.
For equities derive operating EPS from actual income, taxes, non-operating costs
and diluted shares. Do not compound an EPS that ALREADY contains forecast growth.
Use assumptions.operating_eps_bridge with numeric {operating_income,annual_growth,
years,tax_rate,after_tax_nonoperating_cost,diluted_shares} when that method is used.
Formula: (operating_income*(1+annual_growth)^years*(1-tax_rate)
-after_tax_nonoperating_cost)/diluted_shares. All monetary and share inputs must use
consistent scales (e.g. both billions), years is elapsed fiscal growth years, not
necessarily the investment holding period. The cost is an AFTER-tax net cost proxy.
Do not also set eps_bridge. The calculator computes exact EPS and target from those
chosen inputs. Explain assumptions and historical anchors so a reviewer can reproduce.
Do not claim a 40% stablecoin circulation CAGR is 40% EPS growth. For Circle model
reserve return, distribution costs, operating costs, tax and dilution explicitly, or
use a transparently conservative reported operating-income run-rate scenario with
sensitivities; do not sum quarterly EPS with different share weights as an annual EPS.
For ETF use the aligned historical NAV/PE pair only; do not mix latest NAV with an
older PE. A future split does not make a pre-split proposal unusable BEFORE the split:
state a genuine pre-split deadline, then invalidate/recalculate. A requirement to use
post-split data before a pre-split deadline is impossible. Propose a documented
replacement to that authored constraint; never claim the split already occurred.
Preserve actual future earnings-confirmation conditions. Replacing an authored gate
is allowed only with explicit new-proposal justification and independent review.
'''

CONDITION_IDS = '''
CONDITION_IDS_V1. Correct the condition mapping using ONLY original_condition_catalog.
Ignore condition-resolution rows from intermediate failed drafts. Return exactly one
condition_resolution row per catalog condition_id, with condition_id, disposition,
reason and evidence_refs. OMIT previous_condition: the system expands the ID to the
immutable original text BEFORE independent review. Never invent an ID.
For disposition=retained, also list the condition_id in entry.retained_condition_ids;
the system expands these to full entry.conditions before review. Keep actual future
fundamental gates and price_and_fundamental when they remain. Do not call a retained
gate satisfied or move it to invalidation merely to get price_only. A condition can
instead be satisfied_by_evidence only with actual supplied source support.
Preserve sound economic inputs from latest_failed_plan. Correct actual math defects,
but do not invent a higher fair value just to make today's quote look buyable.
Research-only/no-buy is a valid conclusion when the source-supported value is too low.
'''


def condition_catalog(context):
    return [{'condition_id':stable_id('research-entry-condition-v1',text),'text':text}
            for text in ((context.get('prior_plan') or {}).get('entry') or {}).get('conditions',[])]


def expand_condition_ids(proposal,context,*,pre_split_deadline=None):
    """Expand explicit IDs only; never infer semantic matches from similar prose."""
    import copy
    result=copy.deepcopy(proposal)
    catalog={row['condition_id']:row['text'] for row in condition_catalog(context)}
    rows=result.get('condition_resolution')
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
        raise ValueError('Condition ID rows required')
    ids=[row.get('condition_id') for row in rows]
    # This existing engine-enforced quote/cash clause can be supplied by the
    # system and reviewed explicitly. Never infer a missing business condition.
    missing=set(catalog)-set(ids)
    for identity in list(missing):
        text=catalog[identity]
        if (text.startswith('Execution requires a fresh official regular-session quote and verified available cash; policy.amount_usd=')
                and text.endswith(' is a per-buy strategy setting, not proof of cash.')):
            rows.append({'condition_id':identity,'disposition':'execution_gate',
                'reason':'System-supplied mapping of the already enforced official quote and verified cash gate; independent review required.',
                'evidence_refs':[],'system_supplied_execution_mapping':True})
    ids=[row.get('condition_id') for row in rows]
    if len(ids)!=len(catalog) or set(ids)!=set(catalog):
        raise ValueError('Condition IDs must cover the original conditions exactly once')
    for row in rows:
        text=catalog[row['condition_id']]
        if row.get('previous_condition') not in (None,text):
            raise ValueError('Condition ID conflicts with original condition text')
        row['previous_condition']=text
    entry=result.setdefault('entry',{})
    candidates=[]
    if 'retained_condition_ids' in entry:candidates.append(entry.pop('retained_condition_ids'))
    for name in ('retained_condition_ids','entry_retained_condition_ids'):
        if name in result:candidates.append(result.pop(name))
    if candidates and any(value!=candidates[0] for value in candidates[1:]):
        raise ValueError('Conflicting retained-condition ID envelopes')
    retained=candidates[0] if candidates else []
    expected={row['condition_id'] for row in rows if row.get('disposition')=='retained'}
    if not isinstance(retained,list) or len(retained)!=len(expected) or set(retained)!=expected:
        raise ValueError('Retained condition IDs must match the declared dispositions')
    conditions=entry.setdefault('conditions',[])
    for identity in retained:
        if catalog[identity] not in conditions:conditions.append(catalog[identity])
    if pre_split_deadline:
        from .common import parse_time
        day=str(parse_time(pre_split_deadline).date())
        for row in rows:
            text=row['previous_condition']
            if (row.get('disposition')=='retained' and row.get('evidence_refs')
                    and text.startswith('Fundamental discovery: fresh issuer NAV/discount or premium and issuer-published fund P/E')
                    and f'after {day} (post 3:1 split)' in text):
                # An explicit SYSTEM-AUTHORED alternative, not a source fact or
                # a claim that the split occurred. Full review follows expansion.
                row.update(disposition='replaced_authored_constraint',
                    reason='NEW SYSTEM PROPOSAL: the old post-split-only entry timing conflicts with this pre-split price expiry. Permit a pre-split proposal only after rechecking current issuer NAV/premium and aligned valuation units; prohibit entry at a NAV premium. Invalidate at the known split deadline and rederive/review after the split. No future event is claimed satisfied.',
                    system_authored_timing_replacement=True)
                conditions.remove(text)
                conditions.append('Before entry, reverify current issuer NAV and premium/discount and aligned portfolio valuation units; no entry at a market premium to NAV. This PRE-SPLIT proposal expires at '+str(pre_split_deadline)+'; after that deadline all prices require recalculation and independent review.')
                result.setdefault('valuation_basis',{})['system_timing_proposal']={
                    'source_condition_id':row['condition_id'],'deadline':str(pre_split_deadline),
                    'independent_review_required':True,'split_has_occurred':False}
    return result

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
            and (baseline.get('status')=='active' or (baseline.get('research_assessment') or {}).get('status') in ('approved_conditional','approved_no_buy'))
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
    authored=review_authored_proposals(store,model,now)
    result['processed']+=authored['processed']
    result['errors'].extend(authored['errors']);result['deferred'].extend(authored['deferred'])
    return result


def review_authored_proposals(store,model,now):
    """Durable review-only queue for explicit system-authored corrected inputs."""
    from .research import plan_fingerprint
    result={'processed':0,'errors':[],'deferred':[]}
    for key,proposal in store.items('research_authored_proposal'):
        state=store.get('research_authored_review',key) or {}
        if state.get('terminal'):continue
        context=proposal['context'];candidate=context['review_only_candidate']
        source=store.get('research_evidence',proposal['source_id'])
        evidence=store.get('fundamental_evidence',proposal['fundamental_evidence_id'])
        current=store.get('plan',candidate['id'])
        if (not source or not evidence or not current or plan_fingerprint(candidate)!=context['candidate_hash']
                or not same_published_seed(current,context)):
            store.put('research_authored_review',key,{'terminal':True,'status':'superseded_or_invalid_input'})
            continue
        if _unresolved_thesis_defer(store,current,source,{candidate['symbol']:evidence['data']},now):
            result['deferred'].append({'symbol':candidate['symbol'],'reason':'UNRESOLVED_VERIFIED_THESIS_RISK'});continue
        if any(not store.get('model_receipt',k) and not store.get('model_rejection',k) for k,_ in store.items('model_call')):
            result['deferred'].append({'symbol':candidate['symbol'],'error':'UnknownModelOutcome'});continue
        levels=[v for _,v in store.items('research_level') if v.get('symbol')==candidate['symbol'] and source['id'] in v.get('evidence_refs',[])]
        try:
            plans=compile_plan(store,source,levels,model,{candidate['symbol']:evidence['data']},proposal['as_of'],context=context)
        except Exception as error:
            deferred=isinstance(error,(ModelBudgetExceeded,UnknownModelOutcome))
            store.put('research_authored_review',key,{'terminal':not deferred,'status':'deferred' if deferred else 'review_error',
                'error':type(error).__name__})
            result['deferred' if deferred else 'errors'].append({'symbol':candidate['symbol'],'error':type(error).__name__})
            continue
        chosen=next((p for p in plans if p['symbol']==candidate['symbol']),None)
        store.put('research_authored_review',key,{'terminal':True,'status':(chosen or {}).get('research_assessment',{}).get('status','no_result'),
            'symbol':candidate['symbol'],'revision':(chosen or {}).get('revision'),'timestamp':now})
        result['processed']+=1
    return result


def update_research_followups(store,now):
    """No-buy is an interim result; only a verified priced entry closes follow-up."""
    from .common import validate_plan
    from .decisions import _has_provenance
    from .abstention import current_decision
    summary={'researching':0,'ready':0,'approved_no_buy':0,'awaiting_evidence':0,'reviewed_abstentions':0}
    for _,plan in store.items('plan'):
        if plan.get('status') in {'superseded','invalidated'}:continue
        symbol=plan['symbol']
        followup=store.get('research_followup',symbol) or {'symbol':symbol,'created_at':now,'status':'researching'}
        if followup.get('status') not in {'researching','ready'}:continue
        ready=(plan.get('status')=='active' and (plan.get('research_assessment') or {}).get('status')=='approved'
               and not validate_plan(plan) and _has_provenance(store,plan))
        progress={'last_plan_revision':plan.get('revision'),'last_fundamental_evidence_id':plan.get('fundamental_evidence_id'),
                  'last_assessment':(plan.get('research_assessment') or {}).get('status'),
                  'status':'ready' if ready else 'researching',
                  'source_id':plan.get('research_snapshot_id'),
                  'system_owned':True}
        cached=store.get('fundamental_cache',symbol) or {};data=cached.get('data') or {}
        gaps=[g for g in plan.get('research_gaps',[]) if isinstance(g,dict) and g.get('blocking')]
        progress.update(evidence_gaps=gaps, primary_gaps=data.get('primary_gaps',[]),
            next_public_evidence_check=data.get('primary_refresh_after'),
            next_action='monitor_entry' if ready else 'refresh_public_evidence_then_review',
            max_semantic_feedback_attempts=1,
            repeat_same_evidence_paid_calls_allowed=False)
        summary['ready' if ready else 'researching']+=1
        if progress['last_assessment']=='approved_no_buy':summary['approved_no_buy']+=1
        if current_decision(plan):summary['reviewed_abstentions']+=1
        if gaps or progress['primary_gaps']:summary['awaiting_evidence']+=1
        if ready:progress.update(status='ready',completed_at=now)
        if any(followup.get(key)!=value for key,value in progress.items()):
            store.put('research_followup',symbol,dict(followup,**progress))
    return summary


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
        parent_key=key
        replacement_contract=CONTRACT
        if job and job.get('terminal'):
            # A distinct, fixed policy addresses proven economics defects only.
            # Never reset the completed rounds, create timestamped identities, or
            # spend on an already accepted conditional baseline.
            mapping_error=any('Condition resolution' in str(issue) or 'Retained entry condition' in str(issue)
                              for issue in current.get('review_issues',[]))
            if (not economic_consistency_errors(current) and not mapping_error) or (current.get('research_assessment') or {}).get('status') in ('approved_conditional','approved_no_buy'):
                continue
            key=stable_id(ECONOMIC_REPAIR_CONTRACT,parent_key)
            job=store.get('research_system_repair',key)
            replacement_contract=ECONOMIC_REPAIR_CONTRACT
            if job and job.get('terminal'):
                if not mapping_error:continue
                key=stable_id(CONDITION_REPAIR_CONTRACT,parent_key)
                job=store.get('research_system_repair',key)
                replacement_contract=CONDITION_REPAIR_CONTRACT
                if job and job.get('terminal'):
                    if (job.get('status')=='system_error' and job.get('error')=='ValueError'
                            and not job.get('plan_revision') and not job.get('local_envelope_recovery')):
                        # Replay the SAME frozen compile operation/cache; never
                        # create a second paid draft identity for field relocation.
                        job.update(terminal=False,status='local_envelope_recovery',
                                   local_envelope_recovery='retained-condition-envelope-v1')
                        store.put('research_system_repair',key,job)
                    else:continue
        if not job:
            job={'id':key,'contract':replacement_contract,'symbol':symbol,'source_id':source['id'],
                 'generation':generation,'round':0,'max_rounds':1 if replacement_contract==CONDITION_REPAIR_CONTRACT else MAX_ROUNDS,'timestamp':now,
                 'status':'queued','terminal':False}
            if key!=parent_key:job['parent_job_id']=parent_key
            store.put('research_system_repair',key,job)
        store.put('research_system_repair_current',current['id'],job)
        for round_index in range(job['round'],job['max_rounds']):
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
                if not first and job.get('parent_job_id'):
                    first=store.get('research_system_repair_input',stable_id(job['parent_job_id'],'round',0))
                original=(first or {}).get('prior_plan') or latest
                context={'event_id':phase_id,'kind':CONTRACT,'round':round_index,
                         'prior_plan':original,'latest_failed_plan':latest,'system_owned':True}
                if job.get('contract') in (ECONOMIC_REPAIR_CONTRACT,CONDITION_REPAIR_CONTRACT):
                    from .research import _calculate_target_prices
                    context.update(repair_policy=job['contract'],
                        calculation_feedback={'economic_errors':economic_consistency_errors(latest),
                            'calculation_book':_calculate_target_prices(latest)[1]})
                    if job['contract']==CONDITION_REPAIR_CONTRACT:
                        context['original_condition_catalog']=condition_catalog(context)
                store.put('research_system_repair_input',phase_id,context,immutable=True)
            levels=[level for _,level in store.items('research_level') if level.get('symbol')==symbol
                    and source['id'] in level.get('evidence_refs',[])]
            job.update(round=round_index,status='processing');store.put('research_system_repair',key,job)
            if job.get('local_envelope_recovery'):
                store.put('research_local_draft_replay',phase_id,{'contract':job['local_envelope_recovery'],
                    'event_id':phase_id,'paid_generation_allowed':False},immutable=True)
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
            accepted=revised.get('status')=='active' or (revised.get('research_assessment') or {}).get('status') in ('approved_conditional','approved_no_buy')
            job.update(round=round_index+1,plan_revision=revised['revision'],
                       status='completed' if accepted else 'needs_system_review')
            summary['processed']+=1
            if accepted or round_index+1==job['max_rounds']:
                job['terminal']=True
            store.put('research_system_repair',key,job)
            store.put('research_system_repair_current',current['id'],job)
            if job['terminal']:break
        summary['results'].append({'symbol':symbol,'status':job['status'],'round':job['round']})
    return summary
