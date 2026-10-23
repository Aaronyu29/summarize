"""Immutable manual ledger. Staging never records a real fill.

Moving-average costs are a research management convention, not tax accounting.
Cash starts unknown until an explicit opening_cash baseline is confirmed.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from decimal import Decimal

from .common import decimal_value, parse_time, stable_id, utc_now

TYPES = {"opening_cash", "opening_position", "buy", "sell", "deposit", "withdrawal", "dividend", "split", "correction"}


def _number(entry, name, *, optional=False, positive=False):
    value = entry.get(name)
    if optional and (value is None or value == ""):
        entry[name] = None
        return None
    value = decimal_value(value, name)
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")
    entry[name] = str(value)
    return value


def _normalize(entry, *, replacement=False):
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    entry = dict(entry)
    for key in ("status", "confirmed", "confirmed_at"):
        entry.pop(key, None)
    if not isinstance(entry.get("id"), str) or not entry["id"].strip():
        raise ValueError("entry id required")
    if entry.get("account") not in {"real", "paper"}:
        raise ValueError("account must be real or paper")
    if entry.get("type") not in TYPES:
        raise ValueError("unsupported ledger type")
    entry["timestamp"] = parse_time(entry.get("timestamp")).isoformat()
    if entry.get("currency") != "USD":
        raise ValueError("ledger supports USD only; other currencies require reconciled conversion")
    kind = entry["type"]
    if kind in {"opening_position", "buy", "sell", "dividend", "split"}:
        symbol = str(entry.get("symbol", "")).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or symbol.endswith(".US"):
            raise ValueError("bare US symbol required")
        entry["symbol"] = symbol
    if kind in {"opening_position", "buy", "sell"}:
        quantity = _number(entry, "quantity", positive=True)
        price = _number(entry, "price", optional=kind == "opening_position", positive=True)
        _number(entry, "fee", optional=True)
        if kind == "opening_position":
            basis = _number(entry, "cost_basis", optional=True)
            if basis is not None and price is not None and basis != quantity * price:
                raise ValueError("opening cost_basis conflicts with quantity × price")
            start = entry.get("first_fill_at") or entry.get("opened_at") or entry.get("first_opened_at")
            if start:
                start = parse_time(start)
                if start > parse_time(entry["timestamp"]):
                    raise ValueError("opening position acquisition time exceeds baseline time")
                entry["first_fill_at"] = start.isoformat()
    if kind in {"opening_cash", "deposit", "withdrawal", "dividend"}:
        _number(entry, "amount")
        if kind == "dividend":
            _number(entry, "fee", optional=True)
    if kind == "split":
        _number(entry, "ratio", positive=True)
    if "cash_impact" in entry and not isinstance(entry["cash_impact"], bool):
        raise ValueError("cash_impact must be boolean")
    if kind in {"buy", "sell"} and entry.get("cash_impact") is False:
        raise ValueError("buy/sell must include cash movement; use opening_position for a baseline")
    if kind == "correction":
        if replacement:
            raise ValueError("nested correction is unsupported")
        if not entry.get("corrects_id"):
            raise ValueError("correction requires corrects_id")
        if "replacement" not in entry:
            raise ValueError("correction requires replacement (null explicitly voids)")
        if entry["replacement"] is not None:
            revised = dict(entry["replacement"])
            revised.setdefault("id", entry["corrects_id"])
            revised.setdefault("account", entry["account"])
            if revised["id"] != entry["corrects_id"] or revised["account"] != entry["account"]:
                raise ValueError("correction cannot change identity or account")
            entry["replacement"] = _normalize(revised, replacement=True)
    return entry


def stage_entry(store, entry):
    entry = _normalize(entry)
    store.put("ledger_staged", entry["id"], entry, immutable=True)
    return {**entry, "status": "staged"}


def _effective(events, account):
    originals = {}
    order = {}
    corrections = []
    for seq, raw in enumerate(events):
        item = _normalize(raw)
        if item["account"] != account:
            continue
        if item["type"] == "correction":
            corrections.append(item)
        else:
            originals[item["id"]] = item
            order[item["id"]] = seq
    for item in corrections:
        target = item["corrects_id"]
        if target not in originals:
            raise ValueError("correction target must be a confirmed entry in the same account")
        originals[target] = item["replacement"]
    return sorted((item for item in originals.values() if item is not None),
                  key=lambda item: (parse_time(item["timestamp"]), order[item["id"]]))


def confirm_entry(store, id):
    """Explicit caller action. Replay the complete account before atomic append."""
    with store.transaction():
        staged = store.get("ledger_staged", id)
        if staged is None:
            raise ValueError("entry was not staged")
        entry = _normalize(staged)
        existing = next((e for e in store.events("ledger") if e["id"] == id), None)
        if existing is not None:
            if existing != entry:
                raise ValueError("confirmed entry conflicts with staged entry")
            return {**entry, "status": "confirmed"}
        events = store.events("ledger")
        if entry["type"] == "correction":
            original = next((e for e in events if e["id"] == entry["corrects_id"] and e["account"] == entry["account"]), None)
            if original is None or original["type"] == "correction":
                raise ValueError("original confirmed same-account entry required")
            if parse_time(entry["timestamp"]) < parse_time(original["timestamp"]):
                raise ValueError("correction cannot precede the original entry")
        _replay(_effective(events + [entry], entry["account"]))
        store.append_event("ledger", id, entry)
        recorded_at = utc_now()
        store.append_event("ledger_confirmation", id, {"entry_id": id, "account": entry["account"],
            "timestamp": recorded_at, "recorded_at": recorded_at})
    return {**entry, "status": "confirmed"}


def correct_entry(store, original_id, replacement, *, id=None, timestamp=None, reason=None):
    """Stage an auditable replacement or void; caller must confirm separately."""
    original = next((e for e in store.events("ledger") if e["id"] == original_id), None)
    if original is None or original["type"] == "correction":
        raise ValueError("original confirmed entry required")
    stamp = timestamp or utc_now()
    payload = {"id": id or stable_id("correction", original_id, replacement, stamp),
               "account": original["account"], "type": "correction", "timestamp": stamp,
               "currency": "USD", "corrects_id": original_id,
               "replacement": replacement, "reason": reason}
    return stage_entry(store, payload)


def _replay(entries):
    zero = Decimal(0)
    cash = None
    baseline = False
    cash_complete = False
    positions = {}
    gross_realized = zero
    net_realized = zero
    fees_complete = True
    cost_complete = True
    dividend_income = zero
    dividend_income_net = zero
    def change_cash(amount):
        nonlocal cash, cash_complete
        if amount is None:
            cash = None
            cash_complete = False
        elif cash is not None:
            cash += amount
            if cash < 0:
                raise ValueError("confirmed entries exceed known cash")
    for e in entries:
        kind = e["type"]
        fee = decimal_value(e["fee"]) if e.get("fee") is not None else None
        if kind in {"opening_position", "buy", "sell", "dividend"} and fee is None:
            fees_complete = False
        if kind == "opening_cash":
            if baseline:
                raise ValueError("only one opening_cash baseline allowed; use correction")
            if any(x["type"] in {"buy", "sell", "deposit", "withdrawal", "dividend"} for x in entries[:entries.index(e)]):
                raise ValueError("opening_cash must precede cash activity")
            cash = decimal_value(e["amount"])
            baseline = cash_complete = True
            continue
        if kind in {"deposit", "withdrawal"}:
            change_cash(decimal_value(e["amount"]) * (1 if kind == "deposit" else -1))
            continue
        symbol = e.get("symbol")
        p = positions.setdefault(symbol, {"symbol": symbol, "quantity": zero, "cost_basis": zero,
                    "cost_basis_net": zero, "realized_pnl_gross": zero, "realized_pnl_net": zero,
                    "dividend_income": zero, "dividend_income_net": zero, "first_opened_at": None, "observed_since": None,
                    "holding_start_source": None})
        if kind in {"opening_position", "buy"}:
            qty = decimal_value(e["quantity"])
            cost = decimal_value(e["cost_basis"]) if e.get("cost_basis") is not None else qty * decimal_value(e["price"]) if e.get("price") is not None else None
            net_cost = cost + fee if cost is not None and fee is not None else None
            if p["quantity"] == 0:
                p["first_opened_at"] = e["timestamp"] if kind == "buy" else e.get("first_fill_at")
                p["observed_since"] = e["timestamp"]
                p["holding_start_source"] = "confirmed_buy" if kind == "buy" else "user_acquisition_date" if e.get("first_fill_at") else "unknown_opening_acquisition_date"
            p["quantity"] += qty
            for field, amount in (("cost_basis", cost), ("cost_basis_net", net_cost)):
                p[field] = p[field] + amount if p[field] is not None and amount is not None else None
            if cost is None:
                cost_complete = False
            if kind == "buy":
                change_cash(-net_cost if net_cost is not None else None)
        elif kind == "sell":
            qty = decimal_value(e["quantity"])
            if qty > p["quantity"]:
                raise ValueError(f"sell exceeds confirmed {symbol} quantity")
            proceeds = qty * decimal_value(e["price"])
            ratio = qty / p["quantity"]
            gross_cost = p["cost_basis"] * ratio if p["cost_basis"] is not None else None
            net_cost = p["cost_basis_net"] * ratio if p["cost_basis_net"] is not None else None
            gross = proceeds - gross_cost if gross_cost is not None else None
            net = proceeds - fee - net_cost if fee is not None and net_cost is not None else None
            for field, amount in (("realized_pnl_gross", gross), ("realized_pnl_net", net)):
                p[field] = p[field] + amount if p[field] is not None and amount is not None else None
            gross_realized = gross_realized + gross if gross_realized is not None and gross is not None else None
            net_realized = net_realized + net if net_realized is not None and net is not None else None
            p["quantity"] -= qty
            p["cost_basis"] = p["cost_basis"] - gross_cost if gross_cost is not None else None
            p["cost_basis_net"] = p["cost_basis_net"] - net_cost if net_cost is not None else None
            if p["quantity"] == 0:
                p["cost_basis"] = p["cost_basis_net"] = zero
                p["first_opened_at"] = p["observed_since"] = p["holding_start_source"] = None
            change_cash(proceeds - fee if fee is not None else None)
        elif kind == "dividend":
            amount = decimal_value(e["amount"])
            dividend_income += amount
            p["dividend_income"] += amount
            net_income = amount - fee if fee is not None else None
            dividend_income_net = dividend_income_net + net_income if dividend_income_net is not None and net_income is not None else None
            p["dividend_income_net"] = p["dividend_income_net"] + net_income if p["dividend_income_net"] is not None and net_income is not None else None
            change_cash(amount - fee if fee is not None else None)
        elif kind == "split":
            if p["quantity"] <= 0:
                raise ValueError("split requires an existing position")
            p["quantity"] *= decimal_value(e["ratio"])
    return {"cash": cash, "cash_complete": cash_complete, "cost_complete": cost_complete,
            "fees_complete": fees_complete, "positions": positions, "realized_pnl_gross": gross_realized,
            "realized_pnl_net": net_realized, "dividend_income": dividend_income,
            "dividend_income_net": dividend_income_net,
            "total_realized_income_gross": gross_realized + dividend_income if gross_realized is not None else None,
            "total_realized_income_net": net_realized + dividend_income_net if net_realized is not None and dividend_income_net is not None else None,
            "realized_pnl_scope": "trades_only_dividends_separate"}


def portfolio(store, account="real", quotes=None, now=None):
    if account not in {"real", "paper"}:
        raise ValueError("unknown account")
    stamp = parse_time(now or utc_now())
    # `now` is economic time, not an information cutoff. A late confirmed fill
    # restates earlier holdings; immutable recommendations/reports preserve what
    # was known then. Actual receipt times live in ledger_confirmation.
    events = [e for e in store.events("ledger") if parse_time(e["timestamp"]) <= stamp]
    effective = [e for e in _effective(events, account) if parse_time(e["timestamp"]) <= stamp]
    result = _replay(effective)
    account_control = store.get("account_control", account) or {}
    if account == "real":
        holdings_complete = account_control.get("confirmed") is True and account_control.get("holdings_complete") is True
        holdings_basis = "user_confirmed_inventory" if holdings_complete else "inventory_not_reconciled"
    else:
        holdings_complete = any(e["type"] == "opening_cash" and e.get("id") == "paper:opening-cash:v1"
            and e.get("source") == "explicit-paper-model-v1" for e in effective)
        holdings_basis = "paper_model_known_empty_opening" if holdings_complete else "paper_inventory_not_initialized"
    event_ids = {e["id"] for e in events if e["account"] == account}
    receipts = [e for e in store.events("ledger_confirmation")
                if e.get("account") == account and e.get("entry_id") in event_ids]
    recorded = [parse_time(e.get("recorded_at") or e["timestamp"]) for e in receipts]
    result.update({"account": account, "currency": "USD", "as_of": stamp.isoformat(),
                   "target_total_usd": "20000", "drawdown_limit": None,
                   "drawdown_status": "unconfigured", "cost_method": "moving_average_research"})
    result.update({"history_basis": "restated_economic_time",
                   "holdings_complete": holdings_complete, "holdings_basis": holdings_basis,
                   "latest_recorded_at": max(recorded).isoformat() if recorded else None,
                   "contains_late_confirmations": any(t > stamp for t in recorded),
                   "confirmation_count": len(receipts)})
    if isinstance(quotes, list):
        quotes = {q["symbol"]: q for q in quotes}
    quotes = quotes or {}
    positions = []
    market_total = Decimal(0)
    for p in result["positions"].values():
        quote = quotes.get(p["symbol"])
        price, metadata = _valuation_quote(quote, stamp)
        p.update(metadata)
        qty = p["quantity"]
        p["first_fill_at"] = p["opened_at"] = p["first_opened_at"]
        p["holding_days"] = (stamp - parse_time(p["first_opened_at"])).days if p["first_opened_at"] else None
        p["average_cost"] = p["cost_basis"] / qty if qty > 0 and p["cost_basis"] is not None else None
        p["average_cost_net"] = p["cost_basis_net"] / qty if qty > 0 and p["cost_basis_net"] is not None else None
        p["market_price"] = price
        p["market_value"] = qty * price if price is not None else Decimal(0) if qty == 0 else None
        for suffix, basis in (("gross", "cost_basis"), ("net", "cost_basis_net")):
            p[f"unrealized_pnl_{suffix}"] = p["market_value"] - p[basis] if p["market_value"] is not None and p[basis] is not None else None
        market_total = market_total + p["market_value"] if market_total is not None and p["market_value"] is not None else None
        positions.append(p)
    result["positions"] = sorted(positions, key=lambda p: p["symbol"])
    result["market_value"] = market_total
    result["known_holdings_market_value"] = market_total
    result["market_value_scope"] = "complete_reconciled_inventory" if holdings_complete else "recorded_positions_only_inventory_incomplete"
    result["total_equity"] = result["cash"] + market_total if holdings_complete and result["cash"] is not None and market_total is not None else None
    equity = result["total_equity"]
    for p in positions:
        p["portfolio_weight"] = p["market_value"] / equity if equity is not None and equity > 0 and p["market_value"] is not None else None
        p["exposure_weight"] = p["portfolio_weight"]
        p["target_weight"] = p["market_value"] / Decimal("20000") if p["market_value"] is not None else None
    reserved = Decimal(0)
    reservations_complete = True
    for _, reservation in store.items("reservation"):
        if reservation.get("account") != account or reservation.get("status") != "active":
            continue
        try:
            if parse_time(reservation["expires_at"]) > stamp:
                amount = decimal_value(reservation["amount_usd"])
                if amount < 0:
                    raise ValueError()
                reserved += amount
        except (KeyError, ValueError, TypeError):
            reservations_complete = False
    result["planning_reserved_cash"] = reserved if reservations_complete else None
    result["available_cash_after_reservations"] = result["cash"] - reserved if reservations_complete and result["cash"] is not None else None
    result["remaining_cash"] = result["cash"]
    result["cash_weight"] = result["cash"] / equity if equity is not None and equity > 0 else None
    result["invested_weight"] = market_total / equity if equity is not None and equity > 0 else None
    result["valuation_complete"] = holdings_complete and market_total is not None
    result["valuation_status"] = "inventory_incomplete" if not holdings_complete else "historical" if any(p["valuation_status"] == "historical" for p in positions) else "current" if all(p["valuation_status"] == "current" or p["quantity"] == 0 for p in positions) else "incomplete"
    return _serialize(result)


def _valuation_quote(raw, stamp):
    quote = raw.get("selected", raw) if isinstance(raw, dict) else {}
    meta = {"quote_source": quote.get("source"), "quote_status": quote.get("status"),
            "quote_timestamp": quote.get("quote_time", quote.get("timestamp")),
            "quote_session": quote.get("session"), "quote_currency": quote.get("currency"),
            "quote_age_seconds": None, "quote_delayed": quote.get("delayed"),
            "quote_permission_verified": quote.get("permission_verified", raw.get("permission_verified") if isinstance(raw, dict) else None),
            "valuation_status": "unverified", "valuation_reasons": []}
    reasons = meta["valuation_reasons"]
    try:
        timestamp = parse_time(meta["quote_timestamp"])
        meta["quote_age_seconds"] = str(Decimal(str((stamp - timestamp).total_seconds())))
        if timestamp > stamp:
            reasons.append("future_quote")
        if quote.get("status") == "current" and (stamp - timestamp).total_seconds() > 120:
            reasons.append("stale_quote")
    except (ValueError, TypeError):
        reasons.append("unknown_quote_time")
    if quote.get("source") != "longbridge-openapi":
        reasons.append("official_source_required")
    if quote.get("currency") != "USD":
        reasons.append("currency_unverified")
    if quote.get("status") not in {"current", "historical"}:
        reasons.append("valuation_quote_not_current_or_marked_historical")
    if quote.get("status") == "current":
        if meta["quote_permission_verified"] is not True:
            reasons.append("quote_permission_unverified")
        if quote.get("delayed") not in (None, False, 0):
            reasons.append("delayed_quote")
    try:
        price = decimal_value(quote.get("price"))
        if price <= 0:
            raise ValueError()
    except ValueError:
        price = None
        reasons.append("unknown_quote_price")
    if reasons:
        return None, meta
    meta["valuation_status"] = quote["status"]
    return price, meta


def _serialize(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


CSV_COLUMNS = {"成交编号": "id", "账户": "account", "类型": "type", "成交时间": "timestamp",
               "币种": "currency", "股票代码": "symbol", "数量": "quantity", "成交价格": "price",
               "费用": "fee", "金额": "amount", "拆股比例": "ratio", "成本总额": "cost_basis",
               "首次建仓时间": "first_fill_at", "原始建仓时间": "first_fill_at"}
CSV_TYPES = {"期初现金": "opening_cash", "期初持仓": "opening_position", "买入": "buy", "卖出": "sell",
             "入金": "deposit", "出金": "withdrawal", "分红": "dividend", "拆股": "split"}


def import_csv(store, source, account="real", column_map=None):
    """Normalize CSV into staged records; blank fees remain unknown, never zero.

    Explicit execution/fill IDs are required: order IDs cannot identify partial
    fills safely. English canonical or Chinese headers are accepted.
    """
    if hasattr(source, "read"):
        content = source.read()
    elif isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and not "," in source):
        content = Path(source).read_text(encoding="utf-8-sig")
    else:
        content = source
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    mapping = {**CSV_COLUMNS, **(column_map or {})}
    result = {"staged": [], "errors": [], "count": 0}
    for row_number, raw in enumerate(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))), 2):
        try:
            row = {mapping.get(k.strip(), k.strip()): v.strip() if isinstance(v, str) else v
                   for k, v in raw.items() if k is not None and v not in (None, "")}
            row.setdefault("account", account)
            row.setdefault("currency", "USD")
            if row["account"] != account:
                raise ValueError("CSV account conflicts with selected account")
            row["type"] = CSV_TYPES.get(row.get("type"), row.get("type"))
            if not row.get("id"):
                raise ValueError("execution/fill id required (order ID alone is insufficient)")
            result["staged"].append(stage_entry(store, row))
        except (ValueError, TypeError, KeyError) as exc:
            result["errors"].append({"row": row_number, "error": str(exc)})
    result["count"] = len(result["staged"])
    return result
