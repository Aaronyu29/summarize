"""Immutable user-reported broker screens, separate from trades and ledger PnL.

Screenshot acquisition time may be unknown. ``recorded_at`` is the information
receipt clock, never a fabricated quote/fill/screenshot clock. Buying power and
displayed account changes are retained as broker fields, not spendable cash or
investment returns.
"""
from __future__ import annotations

import copy
from decimal import Decimal

from .common import decimal_value, parse_time, stable_id, utc_now
from .market import symbol_value


SUMMARY_AMOUNTS = {
    'cash', 'net_liquidation', 'unrealized_pnl', 'realized_pnl',
    'maintenance_margin', 'excess_liquidity', 'buying_power', 'dividends',
    'account_display_change', 'all_time_display_change',
}
CASH_EVENTS = {'opening_cash', 'buy', 'sell', 'deposit', 'withdrawal', 'dividend'}


def _amount(record, key, *, nonnegative=False):
    if key not in record or record[key] is None:
        return
    value = decimal_value(record[key], key)
    if nonnegative and value < 0:
        raise ValueError(key + ' must be nonnegative')
    record[key] = str(value)


def _normalize(payload):
    if not isinstance(payload, dict):
        raise ValueError('Broker snapshot must be an object')
    value = copy.deepcopy(payload)
    if value.get('account') != 'real' or value.get('broker') != 'IBKR':
        raise ValueError('Broker screenshots support the real IBKR account only')
    if value.get('currency') != 'USD':
        raise ValueError('Explicit USD reporting currency required')
    value['recorded_at'] = parse_time(value.get('recorded_at')).isoformat()
    if value.get('as_of') is not None:
        value['as_of'] = parse_time(value['as_of']).isoformat()
        if parse_time(value['as_of']) > parse_time(value['recorded_at']):
            raise ValueError('Screenshot as_of cannot exceed its receipt time')
    source = value.get('source')
    if (not isinstance(source, dict) or source.get('type') != 'user_screenshot'
            or not isinstance(source.get('reference'), str) or not source['reference'].strip()):
        raise ValueError('User screenshot source reference required')
    summary = value.get('summary')
    if not isinstance(summary, dict) or not summary:
        raise ValueError('Broker summary fields required')
    for key in SUMMARY_AMOUNTS:
        _amount(summary, key)
    # Other screenshot labels remain raw scalar values; nested objects do not
    # silently acquire financial meaning or become execution input.
    if any(isinstance(item, (dict, list, tuple)) for item in summary.values()):
        raise ValueError('Broker summary fields must be scalar')
    rows = value.get('holdings', [])
    if not isinstance(rows, list):
        raise ValueError('Broker holdings must be a list')
    symbols = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Broker holding must be an object')
        if not isinstance(row.get('symbol'), str):
            raise ValueError('Broker holding symbol must be explicit text')
        row['symbol'] = symbol_value(row.get('symbol', ''))
        if row['symbol'] in symbols:
            raise ValueError('Duplicate broker holding symbol')
        symbols.add(row['symbol'])
        if row.get('currency', 'USD') != 'USD':
            raise ValueError('Holding currency conflicts with USD snapshot')
        for key in ('market_value', 'last_price', 'quantity', 'cost_basis', 'average_cost'):
            _amount(row, key, nonnegative=True)
        _amount(row, 'change_percent')
        if row.get('first_fill_at') is not None:
            row['first_fill_at'] = parse_time(row['first_fill_at']).isoformat()
            if parse_time(row['first_fill_at']) > parse_time(value['recorded_at']):
                raise ValueError('Acquisition time exceeds screenshot receipt')
    superseded = value.get('supersedes_symbols', [])
    if not isinstance(superseded, list):
        raise ValueError('Explicit supersedes_symbols list required')
    value['supersedes_symbols'] = sorted({symbol_value(symbol) for symbol in superseded})
    if symbols.intersection(value['supersedes_symbols']):
        raise ValueError('A current holding cannot supersede itself')
    orders = value.get('orders', [])
    if not isinstance(orders, list):
        raise ValueError('Broker order hints must be a list')
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError('Broker order hint must be an object')
        if order.get('symbol') is not None:
            order['symbol'] = symbol_value(order['symbol'])
        for key in ('quantity', 'limit_price'):
            _amount(order, key, nonnegative=True)
    if 'id' not in value:
        value['id'] = stable_id('broker-snapshot-v1', value)
    if not isinstance(value['id'], str) or not value['id'].strip():
        raise ValueError('Broker snapshot id must be nonempty')
    return value


def record_broker_snapshot(store, payload):
    """Persist a screenshot report, without deposits, fills or inventory claims.

    Explicit IDs are recommended for a human screenshot receipt. An omitted ID
    is the canonical payload hash; replay requires the original recorded_at.
    """
    record = _normalize(payload)
    store.put('broker_snapshot', record['id'], record, immutable=True)
    return record


def latest_broker_snapshot(store, account='real', now=None):
    """Select by information receipt time, including when image time is unknown."""
    if account not in {'real', 'paper'}:
        raise ValueError('Unknown account')
    if account == 'paper':
        return None
    cutoff = parse_time(now or utc_now())
    records = []
    for _, raw in store.items('broker_snapshot'):
        if raw.get('account') != account:
            continue
        record = _normalize(raw)
        if parse_time(record['recorded_at']) <= cutoff:
            records.append(record)
    return max(records, key=lambda row: (parse_time(row['recorded_at']), row['id'])) if records else None


def _cash_affecting(event, indexed):
    if event.get('type') in CASH_EVENTS:
        return True
    if event.get('type') == 'correction':
        original = indexed.get(event.get('corrects_id'))
        replacement = event.get('replacement')
        return (original is None or original.get('type') in CASH_EVENTS
            or isinstance(replacement, dict) and replacement.get('type') in CASH_EVENTS)
    return False


def _cash_events_since(store, record, cutoff):
    """A late-entered old economic fill still invalidates a newer cash baseline."""
    events = [item for item in store.events('ledger') if item.get('account') == record['account']]
    indexed = {item['id']: item for item in events}
    receipts = {item.get('entry_id'): item for item in store.events('ledger_confirmation')
        if item.get('account') == record['account']}
    changed = []
    for item in events:
        if not _cash_affecting(item, indexed):
            continue
        receipt = receipts.get(item['id'])
        if receipt:
            at = parse_time(receipt.get('recorded_at') or receipt.get('timestamp'))
            if parse_time(record['recorded_at']) < at <= cutoff:
                changed.append(item['id'])
        elif parse_time(item['timestamp']) <= cutoff:
            # A legacy event without a receipt has no provable information
            # clock. It cannot certify a screenshot's current cash balance.
            changed.append(item['id'])
    return sorted(changed)


def _sum_known_market_values(rows):
    if not rows or any(row.get('market_value') is None for row in rows):
        return None
    return sum((decimal_value(row['market_value']) for row in rows), Decimal(0))


def with_broker_snapshot(store, snapshot):
    """Overlay visibly labelled reported balances; never create ledger entries.

    Cash/NAV overlays stop after any newer confirmed cash event. With unknown
    screenshot time, calculating a delta risks counting a fill twice, so a new
    broker report is required. Historical broker numbers remain visible below
    broker_reported_*; they are never rewritten into ledger PnL.
    """
    result = copy.deepcopy(snapshot)
    account = result.get('account', 'real')
    cutoff = parse_time(result.get('as_of') or utc_now())
    record = latest_broker_snapshot(store, account, cutoff)
    if record is None:
        return result
    summary = record['summary']
    for key, item in summary.items():
        result['broker_reported_' + key] = copy.deepcopy(item)
    rows = copy.deepcopy(record.get('holdings', []))
    for row in rows:
        row.update(quantity_reported=row.get('quantity'), quantity_inferred=False,
            execution_evidence=False, reported_as_of=record.get('as_of'), recorded_at=record['recorded_at'])
    result.update(broker_snapshot_id=record['id'], broker='IBKR',
        broker_account_identifier=record.get('account_identifier', summary.get('account_identifier')),
        broker_snapshot_as_of=record.get('as_of'), broker_snapshot_recorded_at=record['recorded_at'],
        broker_snapshot_source=copy.deepcopy(record['source']), broker_reported_holdings=rows,
        broker_supersedes_symbols=list(record.get('supersedes_symbols', [])),
        broker_reported_summary=copy.deepcopy(summary), broker_reported_orders=copy.deepcopy(record.get('orders', [])),
        broker_order_hints=[dict(order, execution_evidence=False, is_fill_evidence=False,
            execution_status='unknown') for order in record.get('orders', [])],
        broker_pnl_scope='broker_displayed_values_not_ledger_trade_returns',
        broker_buying_power_is_cash=False, broker_reconciled=False)
    known = _sum_known_market_values(rows)
    cash = decimal_value(summary['cash']) if summary.get('cash') is not None else None
    nav = decimal_value(summary['net_liquidation']) if summary.get('net_liquidation') is not None else None
    noncash = nav - cash if nav is not None and cash is not None else None
    result.update(broker_reported_holdings_market_value_sum=str(known) if known is not None else None,
        broker_reported_non_cash_value=str(noncash) if noncash is not None else None,
        broker_reported_unexplained_non_cash_value=str(noncash - known) if noncash is not None and known is not None else None)
    for key in ('cash', 'cash_complete', 'remaining_cash', 'total_equity', 'market_value', 'valuation_status'):
        result.setdefault('ledger_' + key, copy.deepcopy(result.get(key)))
    changed = _cash_events_since(store, record, cutoff)
    result.update(holdings_complete=False, holdings_basis='broker_reported_inventory_not_reconciled',
        cash_complete=False, valuation_complete=False,
        broker_new_cash_event_ids=changed, available_cash_after_reservations=None,
        cash_weight=None, invested_weight=None)
    if any(row.get('cost_basis') is None or row.get('first_fill_at') is None for row in rows):
        result['cost_complete'] = False
    # A balance screen contains no fee receipts, including fees of positions
    # already sold. Displayed zero realized PnL is not evidence of zero fees.
    result['fees_complete'] = False
    for position in result.get('positions', []):
        position['portfolio_weight'] = position['exposure_weight'] = None
    if changed:
        result.update(cash=None, remaining_cash=None, total_equity=None,
            cash_source='broker_snapshot_requires_refresh',
            cash_status='new_ledger_cash_event_requires_broker_refresh',
            total_equity_source='broker_snapshot_requires_refresh', valuation_status='broker_snapshot_requires_refresh')
    else:
        result.update(cash=str(cash) if cash is not None else None,
            remaining_cash=str(cash) if cash is not None else None,
            total_equity=str(nav) if nav is not None else None,
            cash_source='broker_reported_snapshot',
            cash_status='reported_time_unknown' if record.get('as_of') is None else 'reported_unreconciled',
            total_equity_source='broker_reported_net_liquidation', valuation_status='broker_reported_unreconciled')
    return result
