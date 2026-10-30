"""Separate USD 20,000 shadow account using post-signal executable quotes.

No fabricated trigger-price fills. Spread is observed bid/ask, plus a fixed
five basis point adverse slippage and one dollar fee on each fill.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from .common import decimal_value, parse_time, stable_id, utc_now
from .ledger import stage_entry, confirm_entry, portfolio
from .market import quote_gate

SLIPPAGE = Decimal("0.0005")
FEE = Decimal("1")
INITIAL_CASH = Decimal("20000")


def paper_account(store, *, timestamp="2000-01-01T00:00:00+00:00"):
    """Initialize only the separate paper account. Real account remains unknown."""
    with store.transaction():
        init = {"id": "paper:opening-cash:v1", "account": "paper", "type": "opening_cash",
                "timestamp": parse_time(timestamp).isoformat(), "currency": "USD", "amount": str(INITIAL_CASH),
                "source": "explicit-paper-model-v1"}
        stage_entry(store, init)
        confirm_entry(store, init["id"])
    return portfolio(store, "paper")


def _stamp(snapshot):
    return parse_time(snapshot.get("quote_time") or snapshot.get("timestamp") or snapshot.get("as_of") or snapshot.get("quote_at"))


def simulate(store, recommendation, snapshots, now=None):
    """Try the first valid regular-session quote strictly after the signal.

    A recommendation ID can produce at most one fill. Skips are durable
    observations and may be revisited with a later quote. `expires_at` is
    required. Passing `now` makes clock-dependent checks reproducible.
    """
    if not isinstance(recommendation, dict) or not recommendation.get("id"):
        raise ValueError("recommendation id required")
    signal_id = recommendation["id"]
    current = parse_time(now or utc_now())
    with store.transaction():
        source = dict(recommendation)
        store.put("paper_signal", signal_id, source, immutable=True)
        previous = store.get("paper_fill", signal_id)
        if previous:
            return previous
        paper_account(store)
        def skipped(reason, observations=None):
            result = {"id": stable_id("paper-skip", signal_id, reason, observations or []),
                      "recommendation_id": signal_id, "account": "paper", "status": "skipped",
                      "reason": reason, "timestamp": current.isoformat(),
                      "observations": observations or []}
            # Re-evaluations of an unchanged snapshot are idempotent, including
            # the original observation time; do not mutate old audit events.
            old = store.get("paper_observation", result["id"])
            if old:
                return old
            store.put("paper_observation", result["id"], result, immutable=True)
            store.append_event("paper", result["id"], result)
            return result
        if source.get("account") != "paper":
            return skipped("paper_account_recommendation_required")
        try:
            signal_time = parse_time(source.get("timestamp") or source.get("created_at"))
            expiry = parse_time(source.get("expires_at"))
            if expiry <= signal_time:
                return skipped("invalid_signal_expiry")
        except (ValueError, TypeError):
            return skipped("missing_signal_time_or_expiry")
        action = {"add": "buy", "reduce": "sell", "exit": "sell"}.get(source.get("action"), source.get("action"))
        if action not in {"buy", "sell"}:
            return skipped("non_executable_recommendation")
        if source.get("status") not in {"actionable", "active", "recommended"}:
            return skipped("inactive_recommendation")
        symbol = source.get("symbol")
        candidates = []
        rejected = []
        for wrapper in snapshots:
            snapshot = wrapper.get("selected", wrapper)
            if snapshot.get("symbol") != symbol:
                continue
            try:
                quote_time = _stamp(snapshot)
                timestamp = parse_time(snapshot.get("depth_timestamp"))
            except (ValueError, TypeError):
                rejected.append("unknown_quote_or_depth_time")
                continue
            if not signal_time < timestamp <= min(current, expiry):
                rejected.append("quote_not_after_signal_or_outside_validity")
                continue
            if snapshot.get("session") != "regular":
                rejected.append("not_regular_session")
                continue
            gate = quote_gate(wrapper, current.isoformat(), max_age=30)
            if not gate["eligible"] or quote_time > current or snapshot.get("fresh") is False:
                rejected.extend(gate["reasons"] or ["freshness_unverified"])
                continue
            if (snapshot.get("depth_verified") is not True
                    or snapshot.get("depth_source") != "longbridge-openapi"
                    or not 0 <= (current - timestamp).total_seconds() <= 30):
                rejected.append("depth_provenance_or_time_unverified")
                continue
            if snapshot.get("tradable") is False or snapshot.get("halted") is True:
                rejected.append("market_not_tradable")
                continue
            try:
                bid = decimal_value(snapshot.get("bid"))
                ask = decimal_value(snapshot.get("ask"))
                if bid <= 0 or ask <= 0 or ask < bid:
                    raise ValueError()
                if snapshot.get("depth_size_unit") == "shares":
                    for size_field in ("bid_size", "ask_size"):
                        if snapshot.get(size_field) is not None and decimal_value(snapshot[size_field]) <= 0:
                            raise ValueError()
            except ValueError:
                rejected.append("unknown_or_invalid_bid_ask")
                continue
            candidates.append((timestamp, snapshot, bid, ask))
        if not candidates:
            return skipped("no_valid_post_signal_regular_quote", sorted(set(rejected)))
        timestamp, snapshot, bid, ask = min(candidates, key=lambda item: item[0])
        state = portfolio(store, "paper", now=timestamp.isoformat())
        price = ask * (1 + SLIPPAGE) if action == "buy" else bid * (1 - SLIPPAGE)
        try:
            if action == "buy":
                entry_range = source.get("entry") or {}
                try:
                    low = decimal_value(entry_range.get("low"))
                    high = decimal_value(entry_range.get("high"))
                    if low <= 0 or high < low:
                        raise ValueError()
                except ValueError:
                    return skipped("unverified_entry_price_limits")
                if not low <= price <= high:
                    return skipped("post_signal_price_outside_entry_limits")
                budget = decimal_value(source.get("amount_usd", source.get("amount")))
                if not Decimal("1000") <= budget <= Decimal("3000"):
                    return skipped("buy_amount_outside_1000_3000")
                available = decimal_value(state["cash"])
                if available < budget:
                    return skipped("insufficient_paper_cash_for_requested_buy")
                quantity = ((budget - FEE) / price).to_integral_value(rounding=ROUND_FLOOR)
            else:
                requested = decimal_value(source.get("quantity"))
                available_quantity = next((decimal_value(p["quantity"]) for p in state["positions"] if p["symbol"] == symbol), Decimal(0))
                if requested != requested.to_integral_value() or requested <= 0:
                    return skipped("sell_requires_positive_whole_shares")
                if requested > available_quantity:
                    return skipped("insufficient_paper_position")
                quantity = requested
            if quantity <= 0:
                return skipped("insufficient_paper_cash")
            size = snapshot.get("ask_size" if action == "buy" else "bid_size") if snapshot.get("depth_size_unit") == "shares" else None
            if size is not None and quantity > decimal_value(size):
                return skipped("insufficient_displayed_quote_depth")
        except (ValueError, TypeError):
            return skipped("unknown_amount_quantity_or_cash")
        # Exits through gaps use observed bids; entries enforce approved limits.
        ledger_id = stable_id("paper-fill", signal_id)
        entry = {"id": ledger_id, "account": "paper", "type": action, "timestamp": timestamp.isoformat(),
                 "currency": "USD", "symbol": symbol, "quantity": str(quantity), "price": str(price),
                 "fee": str(FEE), "recommendation_id": signal_id, "source": "paper-model-v1"}
        stage_entry(store, entry)
        confirm_entry(store, ledger_id)
        result = {"id": ledger_id, "recommendation_id": signal_id, "account": "paper", "status": "filled",
                  "timestamp": timestamp.isoformat(), "signal_timestamp": signal_time.isoformat(),
                  "action": action, "symbol": symbol, "quantity": str(quantity), "price": str(price),
                  "fee": str(FEE), "amount_usd": str(quantity * price),
                  "cash_impact_usd": str(-(quantity * price + FEE) if action == "buy" else quantity * price - FEE),
                  "observed_bid": str(bid), "observed_ask": str(ask), "slippage_bps": "5",
                  "execution_model": "next_regular_bid_ask_plus_5bps_and_1usd_fee",
                  "liquidity_verified": snapshot.get("depth_size_unit") == "shares" and size is not None,
                  "displayed_size_unit": snapshot.get("depth_size_unit"),
                  "assumed_no_market_impact": True, "depth_time_kind": snapshot.get("depth_time_kind", "observed_at"),
                  "liquidity_scope": "provider_observed_book; no NBBO or actual fillability claim",
                  "quote": snapshot}
        store.put("paper_fill", signal_id, result, immutable=True)
        store.append_event("paper", ledger_id, result)
        return result
