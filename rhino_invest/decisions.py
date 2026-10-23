"""Auditable advice and planning cash reservations. Never submits real trades."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import calendar
from .common import utc_now, parse_time, stable_id, decimal_value, validate_plan
from .market import quote_gate, latest_regular_session


def _number(value):
    try:
        return decimal_value(value)
    except (ValueError, TypeError):
        return None


RISK_SOURCE_KINDS = ('research_evidence', 'fundamental_evidence', 'external_evidence', 'review_event_evidence')


def _risk_source(store, key):
    return next((value for kind in RISK_SOURCE_KINDS if (value := store.get(kind, key)) is not None), None)


def record_thesis_risk(store, plan, assessment, independent_review, *, now=None):
    """Seal an explicit, independently checked match to an existing thesis condition.

    The research producer performs the two separate model calls. This helper
    persists their outputs and exact raw-source hashes; it submits no trade.
    """
    from .research import plan_fingerprint
    if not isinstance(assessment, dict) or not isinstance(independent_review, dict):
        raise ValueError('Thesis risk assessment and independent review must be objects')
    condition = assessment.get('condition')
    refs = assessment.get('evidence_refs')
    review_refs = independent_review.get('evidence_refs')
    if (assessment.get('triggered') is not True or not isinstance(condition, str)
            or condition not in plan.get('invalidation_conditions', [])
            or not isinstance(assessment.get('rationale'), str) or not assessment['rationale'].strip()
            or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
            or independent_review.get('accepted') is not True or independent_review.get('issues')
            or independent_review.get('condition') != condition
            or not isinstance(review_refs, list) or any(not isinstance(ref, str) for ref in review_refs)
            or set(review_refs) != set(refs)
            or not _has_provenance(store, plan)):
        raise ValueError('Thesis risk requires an exact existing condition, original evidence and independent acceptance')
    hashes = {}
    for ref in sorted(set(refs)):
        source = _risk_source(store, ref)
        if source is None:
            raise ValueError('Thesis risk source evidence is not persisted')
        hashes[ref] = stable_id(source)
    assessment_id = stable_id('thesis-risk-assessment', assessment)
    review_id = stable_id('thesis-risk-independent-review', independent_review)
    content = {'plan_id': plan['id'], 'revision': plan['revision'], 'plan_hash': plan_fingerprint(plan),
               'symbol': plan['symbol'], 'condition': condition, 'verified': True,
               'evidence_refs': sorted(hashes), 'evidence_hashes': hashes,
               'assessment_id': assessment_id, 'review_id': review_id}
    identity = stable_id('verified-thesis-risk-v1', content)
    with store.transaction():
        store.put('risk_review', assessment_id, {'output': assessment}, immutable=True)
        store.put('risk_review', review_id, {'output': independent_review}, immutable=True)
        previous = store.get('risk_evidence', identity)
        if previous:
            return previous
        record = dict(content, id=identity, timestamp=parse_time(now or utc_now()).isoformat())
        store.put('risk_evidence', identity, record, immutable=True)
    return record


def valid_thesis_events(store, plan, now=None):
    """Return only sealed two-review risks matching this exact plan revision."""
    from .research import plan_fingerprint
    events = []
    for key, record in store.items('risk_evidence'):
        try:
            if (record.get('verified') is not True or record.get('plan_id') != plan.get('id')
                    or record.get('revision') != plan.get('revision')
                    or record.get('plan_hash') != plan_fingerprint(plan)
                    or record.get('symbol') != plan.get('symbol')
                    or record.get('condition') not in plan.get('invalidation_conditions', [])
                    or (now is not None and parse_time(record['timestamp']) > parse_time(now))):
                continue
            content = {k: v for k, v in record.items() if k not in {'id', 'timestamp'}}
            assessment = (store.get('risk_review', record['assessment_id']) or {}).get('output', {})
            review = (store.get('risk_review', record['review_id']) or {}).get('output', {})
            refs = record.get('evidence_refs') or []
            if (key != record.get('id') or key != stable_id('verified-thesis-risk-v1', content)
                    or record['assessment_id'] != stable_id('thesis-risk-assessment', assessment)
                    or record['review_id'] != stable_id('thesis-risk-independent-review', review)
                    or assessment.get('triggered') is not True or review.get('accepted') is not True
                    or review.get('issues') or assessment.get('condition') != record['condition']
                    or review.get('condition') != record['condition'] or not refs
                    or set(assessment.get('evidence_refs') or []) != set(refs)
                    or set(review.get('evidence_refs') or []) != set(refs)
                    or any(_risk_source(store, ref) is None
                           or stable_id(_risk_source(store, ref)) != record['evidence_hashes'].get(ref) for ref in refs)):
                continue
            events.append(record)
        except (ValueError, TypeError, KeyError):
            continue
    return events


def _month_end(start, months):
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def take_profit_position_basis(store, portfolio_snapshot, symbol, now=None):
    """Current confirmed inventory basis for an explicit independent TP re-review."""
    from .ledger import _effective
    account = portfolio_snapshot.get('account', 'real')
    moment = parse_time(now or portfolio_snapshot.get('as_of') or utc_now())
    positions = portfolio_snapshot.get('positions') or []
    if isinstance(positions, dict):
        positions = positions.values()
    position = next((p for p in positions if p.get('symbol') == symbol), {})
    quantity = _number(position.get('quantity'))
    if quantity is None or quantity <= 0:
        return None
    opened = position.get('first_fill_at') or position.get('first_opened_at') or position.get('observed_since')
    raw = [e for e in store.events('ledger') if e.get('account') == account and parse_time(e['timestamp']) <= moment]
    events = _effective(raw, account)
    relevant = [e for e in events if e.get('symbol') == symbol and e.get('type') in {'buy', 'opening_position', 'split'}
                and (not opened or parse_time(e['timestamp']) >= parse_time(opened))]
    targets = {e['id'] for e in raw if e.get('symbol') == symbol
               and (not opened or parse_time(e['timestamp']) >= parse_time(opened))}
    corrections = [e for e in raw if e.get('type') == 'correction' and e.get('corrects_id') in targets]
    return {'account': account, 'symbol': symbol, 'quantity': str(quantity), 'first_fill_at': opened,
            'structure_hash': stable_id('holding-acquisition-structure', relevant, corrections), 'as_of': moment.isoformat()}


def _has_provenance(store, plan):
    from .research import plan_fingerprint, plan_evidence_refs
    refs = plan_evidence_refs(plan)
    binding = store.get('research_plan_review', plan.get('review_evidence_id', ''))
    if not (binding and binding.get('accepted') is True and binding.get('plan_hash') == plan_fingerprint(plan)
        and set(refs) == set(binding.get('evidence_refs', [])) and refs):
        return False
    for key in refs:
        evidence = next((store.get(kind, key) for kind in ('research_evidence', 'fundamental_evidence',
            'research_level', 'external_evidence') if store.get(kind, key)), None)
        if not evidence or stable_id(evidence) != binding.get('evidence_hashes', {}).get(key):
            return False
    return True


def _linked_fill(store, intent):
    if intent['account'] == 'paper':
        fill = store.get('paper_fill', intent['id'])
        return bool(fill and fill.get('status') == 'filled')
    return any(event.get('account') == intent['account'] and event.get('symbol') == intent.get('symbol')
        and event.get('type') in {'buy', 'sell'}
        and (event.get('recommendation_id') == intent['id']
             or (event.get('tranche_id') == intent.get('tranche') and event.get('plan_id') == intent.get('plan_id')))
        for event in store.events('ledger'))


def evaluate(store, plan, market_snapshot, portfolio_snapshot, now=None):
    moment = parse_time(now or utc_now()).isoformat()
    with store.transaction():
        return _evaluate(store, plan, market_snapshot, portfolio_snapshot, moment)


def _evaluate(store, plan, snapshot, portfolio, moment):
    quote = snapshot.get('selected', snapshot)
    symbol = plan.get('symbol')
    account = portfolio.get('account', 'real')
    control = store.get('plan_control', plan.get('id')) or {}
    batch = str(control.get('tranche_id', 'entry')) if control.get('confirmed') is True else 'entry'
    entry_intent_id = stable_id('advice-intent', account, plan.get('id'), 'entry', batch)
    existing_entry = store.get('advice_intent', entry_intent_id)
    if existing_entry and existing_entry['status'] in {'pending', 'suspended'}:
        if _linked_fill(store, existing_entry):
            existing_entry = dict(existing_entry, status='filled', filled_at=moment)
        elif parse_time(existing_entry['expires_at']) <= parse_time(moment):
            existing_entry = dict(existing_entry, status='expired')
        elif existing_entry.get('revision') != plan.get('revision'):
            existing_entry = dict(existing_entry, status='cancelled', reason='superseded_revision')
        store.put('advice_intent', entry_intent_id, existing_entry)
    if existing_entry and existing_entry['status'] not in {'pending', 'suspended'}:
        reservation = store.get('reservation', stable_id(account, plan.get('symbol')))
        if reservation and reservation.get('intent_id') == entry_intent_id:
            store.put('reservation', reservation['id'], dict(reservation, status='released'))
    positions = portfolio.get('positions') or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    position = next((p for p in positions if p.get('symbol') == symbol), {})
    held = _number(position.get('quantity')) or Decimal(0)
    price = _number(quote.get('price'))
    schema_errors = validate_plan(plan)
    errors = list(schema_errors)
    for key, prior in store.items('advice_intent'):
        if prior.get('account') != account or prior.get('symbol') != symbol or prior.get('status') not in {'pending', 'suspended'} or key == entry_intent_id:
            continue
        if prior.get('tranche', '').startswith('tp:') or prior.get('tranche') == 'exit':
            continue
        if prior.get('plan_id') == plan.get('id') and control.get('confirmed') is True and control.get('tranche_id'):
            store.put('advice_intent', key, dict(prior, status='cancelled', reason='explicit_new_batch_replaced_pending_batch'))
        else:
            errors.append('another_pending_entry_requires_reconciliation')
    reasons = []
    action, status, amount, quantity = ('hold' if held > 0 else 'watch'), 'waiting', None, None
    expiry = snapshot.get('next_regular_close') or quote.get('next_regular_close')
    gate = quote_gate(snapshot, moment)
    if quote.get('symbol', snapshot.get('symbol')) != symbol:
        gate['reasons'].append('quote_symbol_mismatch')
        gate['eligible'] = False
    verified_plan = _has_provenance(store, plan)
    if not verified_plan:
        errors.append('verified stored evidence missing')
    if plan.get('status') != 'active':
        errors.append('plan_not_active')
    if plan.get('conditions_parsed') is not True:
        errors.append('entry_conditions_unverified')
    try:
        from .ledger import _effective
        as_of = parse_time(moment)
        confirmed = [event for event in store.events('ledger') if parse_time(event['timestamp']) <= as_of]
        split_events = [event for event in _effective(confirmed, account)
            if event.get('type') == 'split' and event.get('symbol') == symbol
            and parse_time(plan['created_at']) < parse_time(event['timestamp']) <= as_of]
        if split_events:
            errors.append('post_plan_split_requires_price_unit_review')
    except (ValueError, KeyError, TypeError):
        errors.append('corporate_action_history_unverifiable')
    account_control = store.get('account_control', account) or {}
    drawdown_limit = (_number(account_control.get('drawdown_limit'))
        if account_control.get('confirmed') is True else None)
    if drawdown_limit is not None and not Decimal(0) < drawdown_limit < Decimal(1):
        errors.append('drawdown_limit_invalid')
    equity, peak = _number(portfolio.get('total_equity')), _number(portfolio.get('peak_equity'))
    drawdown = max(Decimal(0), (peak - equity) / peak) if peak and peak > 0 and equity is not None else None
    drawdown_status = ('unconfigured' if drawdown_limit is None else 'not_evaluable' if drawdown is None
        else 'breached' if drawdown >= drawdown_limit else 'within_limit')
    due = False
    try:
        due = parse_time(moment) >= parse_time(plan.get('next_review_at'))
    except (ValueError, TypeError):
        errors.append('review_date_missing')
    if due:
        reasons.append('scheduled_thesis_review_due')
    opened = position.get('first_fill_at') or position.get('first_opened_at') or position.get('opened_at')
    holding_days, holding_months = None, None
    if held > 0 and opened:
        try:
            started, current = parse_time(opened), parse_time(moment)
            holding_days = max(0, (current - started).days)
            holding_months = (current.year - started.year) * 12 + current.month - started.month - (current.day < started.day)
            if not plan.get('thesis_valid_from') and holding_months >= plan.get('horizon_months', 36):
                due = True
                reasons.append('investment_horizon_expired_requires_new_thesis')
        except (ValueError, TypeError):
            reasons.append('holding_start_unknown')
    if plan.get('thesis_valid_from') is not None or plan.get('horizon_until') is not None:
        try:
            start, until = parse_time(plan['thesis_valid_from']), parse_time(plan['horizon_until'])
            months = plan.get('horizon_months')
            if (isinstance(months, bool) or not isinstance(months, int) or not 12 <= months <= 36
                    or start != parse_time(plan['created_at']) or start > parse_time(moment)
                    or until != _month_end(start, months)):
                raise ValueError()
            if parse_time(moment) >= until:
                due = True
                reasons.append('investment_horizon_expired_requires_new_thesis')
        except (ValueError, TypeError, KeyError, OverflowError):
            errors.append('reviewed_thesis_horizon_invalid')
    try:
        if not expiry or parse_time(expiry) <= parse_time(moment):
            raise ValueError()
    except (ValueError, TypeError):
        gate['eligible'] = False
        gate['reasons'].append('verified_next_regular_close_required')
    # Bare self-declared snapshot booleans cannot authorize an exit. Risks are
    # read directly from immutable, two-review evidence matching this version.
    invalidated = valid_thesis_events(store, plan, moment)
    reported_held = not position and any(record.get('account', account) == account and record.get('symbol') == symbol
        and (_number(record.get('reported_market_value')) or Decimal(0)) > 0
        for record in portfolio.get('reported_holdings', []))
    stop = plan.get('stop') or {}
    stop_price = _number(stop.get('price'))
    stop_triggered = False
    price_basis_verified = (verified_plan and not schema_errors and plan.get('conditions_parsed') is True
        and 'post_plan_split_requires_price_unit_review' not in errors
        and 'corporate_action_history_unverifiable' not in errors)
    if (held > 0 or reported_held) and stop_price is not None and price_basis_verified:
        mode = stop.get('confirmation', 'daily_close')
        if mode == 'intraday' and gate['eligible'] and price is not None:
            stop_triggered = price <= stop_price
        elif mode in {'daily_close', 'weekly_close'}:
            close = snapshot.get('regular_close') or {}
            closing = _number(close.get('price'))
            try:
                latest = latest_regular_session(moment)
                stop_triggered = (close.get('confirmed') is True and close.get('source') == 'longbridge-openapi'
                    and close.get('currency') == 'USD' and close.get('symbol') == symbol
                    and latest.get('date') == close.get('session_date')
                    and parse_time(close.get('quote_time')) == parse_time(latest.get('close'))
                    and parse_time(close.get('quote_time')) <= parse_time(moment)
                    and closing is not None and closing <= stop_price
                    and (mode != 'weekly_close' or close.get('weekly_confirmed') is True))
            except (ValueError, TypeError, ImportError):
                pass
    if invalidated:
        reasons.append('verified_thesis_invalidation')
        if held > 0 and portfolio.get('ledger_input_current') is not False:
            action, status, quantity = 'exit', 'actionable', held
        elif held > 0 or reported_held:
            action, status, quantity = 'hold' if held > 0 else 'watch', 'warning', None
            reasons.append('ledger_input_not_current' if held > 0 else 'reported_holding_quantity_unknown_requires_risk_check')
        else:
            action, status, quantity = 'watch', 'invalidated', None
    elif stop_triggered:
        reasons.append('confirmed_price_stop')
        if held <= 0:
            action, status, quantity = 'watch', 'warning', None
            reasons.append('reported_holding_quantity_unknown_requires_risk_check')
        elif errors or due or portfolio.get('ledger_input_current') is False:
            action, status, quantity = 'hold', 'warning', None
            reasons.extend(errors)
            if portfolio.get('ledger_input_current') is False:
                reasons.append('ledger_input_not_current')
        else:
            action, status, quantity = 'exit', 'actionable', held
    elif errors:
        status = 'needs_review'
        reasons.extend(errors)
    elif portfolio.get('ledger_input_current') is False:
        status = 'data_blocked'
        reasons.append('ledger_input_not_current')
    elif due:
        status = 'needs_review'
    elif drawdown_status in {'breached', 'not_evaluable'}:
        status = 'needs_review'
        reasons.append('portfolio_drawdown_' + drawdown_status)
    elif not gate['eligible']:
        status = 'data_blocked'
        reasons.extend(gate['reasons'])
    elif held > 0 and price is not None and any(price >= decimal_value(s['price']) for s in plan['take_profit']):
        baseline_key = stable_id(account, plan['id'], opened, 'holding_baseline_v2')
        baseline = store.get('take_profit_anchor', baseline_key)
        current_basis = take_profit_position_basis(store, portfolio, symbol, moment)
        if baseline is None:
            legacy = store.get('advice_baseline', stable_id(account, plan['id'], opened, 'holding_baseline'))
            if legacy:
                baseline = dict(legacy, structure_hash=None, revision=0)
        if baseline is None:
            baseline = dict(current_basis, timestamp=moment, revision=plan['revision'])
            store.put('take_profit_anchor', baseline_key, baseline)
        structure_changed = baseline.get('structure_hash') != current_basis['structure_hash']
        if structure_changed:
            reviewed = (plan.get('take_profit_position_basis') or {}).get(account) or {}
            matched = (plan['revision'] > baseline.get('revision', 0)
                and all(reviewed.get(key) == current_basis.get(key)
                        for key in ('account', 'symbol', 'quantity', 'first_fill_at', 'structure_hash')))
            try:
                matched = matched and parse_time(reviewed['as_of']) <= parse_time(plan['created_at']) <= parse_time(moment)
            except (ValueError, TypeError, KeyError):
                matched = False
            if matched:
                baseline = dict(current_basis, timestamp=moment, revision=plan['revision'])
                store.put('take_profit_anchor', baseline_key, baseline)
            else:
                status = 'needs_review'
                reasons.append('position_structure_changed_requires_take_profit_review')
        if status != 'needs_review':
            base_quantity = decimal_value(baseline['quantity'])
            triggered = [s for s in plan['take_profit'] if price >= decimal_value(s['price'])]
            fraction = sum((decimal_value(s['fraction']) for s in triggered), Decimal(0))
            already_sold = max(Decimal(0), base_quantity - held)
            quantity = min(held, max(Decimal(0), (base_quantity * fraction).to_integral_value(rounding=ROUND_FLOOR) - already_sold))
            if quantity > 0:
                action, status = ('exit' if quantity == held else 'reduce'), 'actionable'
                reasons.append('take_profit_schedule_reached')
            else:
                reasons.append('whole_share_take_profit_below_one_share')
        else:
            quantity = None
    elif price is not None and decimal_value(plan['entry']['low']) <= price <= decimal_value(plan['entry']['high']):
        if account == 'real' and portfolio.get('holdings_complete') is not True:
            status = 'needs_review'
            reasons.append('actual_holdings_not_reconciled')
        elif portfolio.get('cash_complete') is not True or portfolio.get('cost_complete') is not True or portfolio.get('fees_complete') is not True:
            status = 'needs_review'
            reasons.append('actual_account_cash_cost_or_fees_incomplete')
        elif portfolio.get('currency') != 'USD' or _number(portfolio.get('cash')) is None:
            status = 'needs_review'
            reasons.append('account_cash_currency_unverified')
        else:
            selected = control.get('amount_usd') if control.get('confirmed') is True else None
            budget = (_number(selected) if selected is not None else Decimal('1000'))
            pending_rec = store.get('recommendation', entry_intent_id) if existing_entry else None
            if existing_entry and existing_entry['status'] in {'pending', 'suspended'} and pending_rec:
                budget = decimal_value(pending_rec['amount_usd'])
            if budget is None or not Decimal('1000') <= budget <= Decimal('3000'):
                status = 'needs_review'
                reasons.append('user_buy_amount_invalid')
            else:
                reserved = Decimal(0)
                reservation_key = stable_id(account, symbol)
                for key, reservation in store.items('reservation'):
                    if key != reservation_key and reservation.get('account') == account and reservation.get('status') == 'active':
                        if parse_time(reservation['expires_at']) > parse_time(moment):
                            reserved += decimal_value(reservation['amount_usd'])
                available = decimal_value(portfolio['cash']) - reserved
                if available < budget:
                    status = 'cash_blocked'
                    reasons.append('insufficient_unreserved_actual_cash')
                else:
                    # Include estimated fill fee in the fixed outlay ceiling.
                    fee = decimal_value(plan.get('estimated_fee_usd', '1'))
                    quantity = ((budget - fee) / price).to_integral_value(rounding=ROUND_FLOOR)
                    if quantity <= 0:
                        status = 'cash_blocked'
                        reasons.append('budget_below_one_share_plus_fee')
                    else:
                        action, status, amount = ('add' if held > 0 else 'buy'), 'actionable', budget
                        store.put('reservation', reservation_key, {'id': reservation_key, 'account': account,
                            'symbol': symbol, 'plan_id': plan['id'], 'amount_usd': str(budget),
                            'intent_id': entry_intent_id, 'quantity': str(quantity),
                            'expires_at': (existing_entry or {}).get('expires_at', expiry), 'timestamp': moment, 'status': 'active'})
                        reasons.append('verified_entry_zone_and_reserved_cash')
    else:
        reasons.append('outside_entry_zone')
    if action in {'reduce', 'exit'} and status == 'actionable' and not gate['eligible']:
        status = 'warning'
        reasons.extend(['exit_price_not_executable'] + gate['reasons'])
    for key, prior in store.items('advice_intent'):
        if prior.get('plan_id') != plan.get('id') or prior.get('account') != account or prior.get('status') not in {'pending', 'suspended'}:
            continue
        changed = None
        if _linked_fill(store, prior):
            changed = dict(prior, status='filled', filled_at=moment)
        elif parse_time(prior['expires_at']) <= parse_time(moment):
            changed = dict(prior, status='expired')
        elif ((errors or due) and not (invalidated and prior.get('tranche') == 'exit')) or prior.get('revision') != plan.get('revision'):
            changed = dict(prior, status='cancelled', reason='plan_review_or_supersession')
        elif status != 'actionable':
            changed = dict(prior, status='suspended', reason='latest_advice_not_actionable')
        if changed:
            store.put('advice_intent', key, changed)
    # Stable, once-only intents prevent price polling from creating a new tranche.
    intent_id = None
    if action in {'buy', 'add', 'reduce', 'exit'} and status == 'actionable':
        tranche = batch if action in {'buy', 'add'} else ('tp:' + str(len(triggered))
            if 'take_profit_schedule_reached' in reasons else 'exit')
        tp_anchor = baseline.get('structure_hash') if 'take_profit_schedule_reached' in reasons else None
        intent_id = (entry_intent_id if action in {'buy', 'add'} else
            stable_id('advice-intent', account, plan['id'], opened, tranche, tp_anchor) if tp_anchor else
            stable_id('advice-intent', account, plan['id'], opened, tranche))
        old_intent = store.get('advice_intent', intent_id)
        if old_intent:
            if old_intent['status'] in {'pending', 'suspended'} and _linked_fill(store, old_intent):
                store.put('advice_intent', intent_id, dict(old_intent, status='filled', filled_at=moment))
                old_intent['status'] = 'filled'
            if action in {'reduce', 'exit'} and old_intent['status'] in {'pending', 'suspended'} and held != decimal_value(old_intent['baseline_quantity']):
                store.put('advice_intent', intent_id, dict(old_intent, status='cancelled', reason='unlinked_holding_change_requires_reconciliation'))
                old_intent['status'] = 'cancelled'
            if old_intent['status'] in {'pending', 'suspended'} and parse_time(old_intent['expires_at']) <= parse_time(moment):
                store.put('advice_intent', intent_id, dict(old_intent, status='expired'))
                old_intent['status'] = 'expired'
            if old_intent['status'] in {'pending', 'suspended'}:
                store.put('advice_intent', intent_id, dict(old_intent, status='pending'))
                previous = store.get('recommendation', intent_id)
                if previous:
                    return previous
            action, status, amount, quantity = ('hold' if held > 0 else 'watch'), 'waiting', None, None
            reasons.append('tranche_' + old_intent['status'] + '_requires_new_approved_revision')
            intent_id = None
        else:
            store.put('advice_intent', intent_id, {'id': intent_id, 'account': account,
                'plan_id': plan['id'], 'symbol': symbol, 'revision': plan['revision'], 'tranche': tranche,
                'baseline_quantity': str(held), 'quantity': str(quantity), 'expires_at': expiry,
                'timestamp': moment, 'status': 'pending'})
    reservation_key = stable_id(account, symbol)
    if action not in {'buy', 'add'}:
        old = store.get('reservation', reservation_key)
        live = store.get('advice_intent', entry_intent_id)
        if live and live.get('status') in {'pending', 'suspended'}:
            terminal = bool(errors or due or invalidated or stop_triggered)
            live = dict(live, status='cancelled' if terminal else 'suspended')
            store.put('advice_intent', entry_intent_id, live)
        keep_reserved = live and live.get('status') in {'pending', 'suspended'}
        if old and old.get('status') == 'active' and not keep_reserved:
            store.put('reservation', reservation_key, dict(old, status='released', released_at=moment))
    recommendation = {'id': intent_id or stable_id('recommendation', plan.get('id'), plan.get('revision'), account,
            quote, portfolio, action, status, amount, quantity, expiry, reasons),
        'timestamp': moment, 'created_at': moment, 'plan_id': plan.get('id'), 'revision': plan.get('revision'),
        'symbol': symbol, 'account': account, 'action': action, 'status': status, 'reasons': reasons,
        'tranche_id': batch if action in {'buy', 'add'} else None,
        'evidence_refs': plan.get('evidence_refs', []), 'amount_usd': str(amount) if amount is not None else None,
        'quantity': str(quantity) if quantity is not None else None, 'entry': plan.get('entry'),
        'holding_duration_months': plan.get('horizon_months'), 'horizon_months': plan.get('horizon_months'),
        'targets': plan.get('targets', []), 'take_profit': plan.get('take_profit', []), 'stop': stop,
        'invalidation_conditions': plan.get('invalidation_conditions', []),
        'time_exit_policy': plan.get('time_exit_policy'), 'next_review_at': plan.get('next_review_at'),
        'thesis_valid_from': plan.get('thesis_valid_from'), 'horizon_until': plan.get('horizon_until'),
        'risk_evidence_ids': [event['id'] for event in invalidated],
        'take_profit_position_basis': current_basis if 'current_basis' in locals() else None,
        'holding_days': holding_days, 'holding_months_elapsed': holding_months, 'first_fill_at': opened,
        'expires_at': expiry, 'risk_context': {'drawdown_limit': str(drawdown_limit) if drawdown_limit is not None else None,
            'drawdown_status': drawdown_status, 'current_drawdown': str(drawdown) if drawdown is not None else None,
            'risk_check_passed': False, 'cash_source': 'actual_confirmed_ledger', 'planning_only': True},
        'market_gate': gate}
    if price and price > 0 and stop_price is not None:
        unit_risk = max(Decimal(0), price - stop_price)
        recommendation['risk_context'].update(price_risk_per_share_usd=str(unit_risk),
            price_risk_percent=str(unit_risk / price * 100),
            proposed_price_risk_usd=str(unit_risk * quantity) if quantity is not None else None,
            holding_price_risk_usd=str(unit_risk * held))
        recommendation['scenario_returns'] = [{'scenario': t.get('scenario'),
            'return_percent': str((decimal_value(t['price']) / price - 1) * 100),
            'annualized_return_percent': str(round((float(decimal_value(t['price']) / price) ** (12 / t['horizon_months']) - 1) * 100, 4))}
            for t in plan.get('targets', []) if _number(t.get('price')) and isinstance(t.get('horizon_months'), int)
                and not isinstance(t.get('horizon_months'), bool) and 12 <= t['horizon_months'] <= 36]
    # Same recommendation can be observed many times; preserve first audit timestamp.
    previous = store.get('recommendation', recommendation['id'])
    if previous:
        return previous
    store.put('recommendation', recommendation['id'], recommendation, immutable=True)
    return recommendation
