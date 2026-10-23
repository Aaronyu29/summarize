"""Auditable advice and planning cash reservations. Never submits real trades."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from .common import utc_now, parse_time, stable_id, decimal_value, validate_plan
from .market import quote_gate, latest_regular_session


def _number(value):
    try:
        return decimal_value(value)
    except (ValueError, TypeError):
        return None


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
    errors = validate_plan(plan)
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
    if not _has_provenance(store, plan):
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
            if holding_months >= plan.get('horizon_months', 36):
                due = True
                reasons.append('investment_horizon_expired_requires_new_thesis')
        except (ValueError, TypeError):
            reasons.append('holding_start_unknown')
    try:
        if not expiry or parse_time(expiry) <= parse_time(moment):
            raise ValueError()
    except (ValueError, TypeError):
        gate['eligible'] = False
        gate['reasons'].append('verified_next_regular_close_required')
    invalidations = snapshot.get('thesis_events') or []
    invalidated = [e for e in invalidations if e.get('verified') is True
                   and e.get('condition') in plan.get('invalidation_conditions', []) and e.get('evidence_refs')
                   and all(store.get('risk_evidence', ref) for ref in e['evidence_refs'])]
    stop = plan.get('stop') or {}
    stop_price = _number(stop.get('price'))
    stop_triggered = False
    if held > 0 and stop_price is not None:
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
    if errors:
        status = 'needs_review'
        reasons.extend(errors)
    elif portfolio.get('ledger_input_current') is False:
        status = 'data_blocked'
        reasons.append('ledger_input_not_current')
    elif invalidated:
        action, status = ('exit', 'actionable') if held > 0 else ('watch', 'invalidated')
        quantity = held if held > 0 else None
        reasons.append('verified_thesis_invalidation')
    elif stop_triggered:
        action, status, quantity = 'exit', 'actionable', held
        reasons.append('confirmed_price_stop')
    elif due:
        status = 'needs_review'
    elif drawdown_status in {'breached', 'not_evaluable'}:
        status = 'needs_review'
        reasons.append('portfolio_drawdown_' + drawdown_status)
    elif not gate['eligible']:
        status = 'data_blocked'
        reasons.extend(gate['reasons'])
    elif held > 0 and price is not None and any(price >= decimal_value(s['price']) for s in plan['take_profit']):
        baseline_key = stable_id(account, plan['id'], opened, 'holding_baseline')
        baseline = store.get('advice_baseline', baseline_key)
        if baseline is None:
            baseline = {'quantity': str(held), 'timestamp': moment}
            store.put('advice_baseline', baseline_key, baseline, immutable=True)
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
        elif errors or due or prior.get('revision') != plan.get('revision'):
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
        intent_id = entry_intent_id if action in {'buy', 'add'} else stable_id('advice-intent', account, plan['id'], opened, tranche)
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
