"""Shared deterministic identifiers, exact decimal parsing and plan contracts."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp must be ISO-8601 text or datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    return parsed.astimezone(timezone.utc)


def decimal_value(value, name="value") -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def stable_id(*parts) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         default=lambda v: str(v) if isinstance(v, Decimal) else _unsupported(v))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unsupported(value):
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def entry_condition_errors(plan) -> list[str]:
    """Only an independently covered, exact price interval can auto-enter.

    Text is never interpreted as an executable predicate. The independent
    review binds every original condition to the explicit supported rule;
    future fundamental conditions require another reviewed plan revision.
    """
    if not isinstance(plan, dict):
        return ["entry_conditions_unverified"]
    entry = plan.get("entry")
    entry = entry if isinstance(entry, dict) else {}
    conditions = entry.get("conditions")
    predicates = entry.get("execution_conditions")
    review = plan.get("entry_condition_review")
    if (entry.get("condition_mode") != "price_only"
            or not isinstance(conditions, list) or not conditions
            or any(not isinstance(text, str) or not text.strip() for text in conditions)
            or not isinstance(predicates, list) or len(predicates) != 1
            or not isinstance(predicates[0], dict)
            or set(predicates[0]) != {"type", "low", "high"}
            or predicates[0].get("type") != "price_between"
            or not isinstance(review, dict)
            or review.get("condition_mode") != "price_only"
            or review.get("all_text_conditions_covered") is not True
            or review.get("conditions") != conditions
            or review.get("execution_conditions") != predicates):
        return ["entry_conditions_unverified"]
    try:
        low, high = decimal_value(entry.get("low")), decimal_value(entry.get("high"))
        if (low <= 0 or high < low or decimal_value(predicates[0].get("low")) != low
                or decimal_value(predicates[0].get("high")) != high):
            raise ValueError()
    except (TypeError, ValueError):
        return ["entry_conditions_unverified"]
    return []


def validate_plan(plan) -> list[str]:
    """Return actionable omissions; never turn an unparsed condition into a buy.

    Drafts can be stored with these errors. Activation callers must require [].
    Verification is explicit evidence review, not the existence of a URL.
    """
    errors = []
    if not isinstance(plan, dict):
        return ["plan must be an object"]
    if plan.get('research_intent')=='no_buy':
        errors.append('no-buy research cannot authorize execution')
    for field in ("id", "revision", "symbol", "status", "created_at", "thesis",
                  "time_exit_policy", "valuation_basis"):
        if not plan.get(field):
            errors.append(f"missing {field}")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", str(plan.get("symbol", ""))) or str(plan.get("symbol", "")).endswith(".US"):
        errors.append("symbol must be a bare uppercase US symbol")
    if plan.get("status") not in {"candidate", "needs_review", "active", "superseded", "invalidated"}:
        errors.append("invalid status")
    revision = plan.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("revision must be a positive integer")
    for name in ("created_at", "next_review_at"):
        try:
            parse_time(plan.get(name))
        except (ValueError, TypeError):
            errors.append(f"invalid {name}")
    try:
        if parse_time(plan["next_review_at"]) <= parse_time(plan["created_at"]):
            errors.append("next_review_at must follow created_at")
    except (ValueError, TypeError, KeyError):
        pass
    def horizon(value, label):
        if isinstance(value, bool) or not isinstance(value, int) or not 12 <= value <= 36:
            errors.append(f"{label} must be 12–36 months")
    horizon(plan.get("horizon_months"), "horizon_months")
    def positive(value, label):
        try:
            number = decimal_value(value, label)
            if number <= 0:
                raise ValueError()
            return number
        except ValueError:
            errors.append(f"{label} must be positive")
            return None
    if not isinstance(plan.get("evidence_refs"), list) or not plan["evidence_refs"]:
        errors.append("evidence_refs required")
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    low = positive(entry.get("low"), "entry.low")
    high = positive(entry.get("high"), "entry.high")
    amount = positive(entry.get("amount_usd"), "entry.amount_usd")
    if low is not None and high is not None and low > high:
        errors.append("entry.low exceeds entry.high")
    if amount is not None and not Decimal("1000") <= amount <= Decimal("3000"):
        errors.append("each buy amount must be 1000–3000 USD")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        errors.append("evidenced target scenarios required")
    else:
        for i, target in enumerate(targets):
            if not isinstance(target, dict):
                errors.append(f"targets[{i}] must be an object")
                continue
            positive(target.get("price"), f"targets[{i}].price")
            horizon(target.get("horizon_months"), f"targets[{i}].horizon_months")
            for field in ("scenario", "method", "assumptions", "evidence_refs"):
                if not target.get(field):
                    errors.append(f"targets[{i}].{field} required")
    schedule = plan.get("take_profit")
    if not isinstance(schedule, list) or not schedule:
        errors.append("take_profit schedule required")
    else:
        total = Decimal(0)
        for i, step in enumerate(schedule):
            if not isinstance(step, dict):
                errors.append(f"take_profit[{i}] must be an object")
                continue
            positive(step.get("price"), f"take_profit[{i}].price")
            fraction = positive(step.get("fraction"), f"take_profit[{i}].fraction")
            if fraction is not None:
                total += fraction
        if total > 1:
            errors.append("take_profit fractions exceed one")
    stop = plan.get("stop") if isinstance(plan.get("stop"), dict) else {}
    stop_price = positive(stop.get("price"), "stop.price")
    if stop.get("confirmation") not in {"intraday", "daily_close"}:
        errors.append("automatic stop confirmation currently supports intraday/daily_close only")
    if stop_price is not None and low is not None and stop_price >= low:
        errors.append("stop.price must be below entry.low")
    if not isinstance(plan.get("invalidation_conditions"), list) or not plan["invalidation_conditions"] or any(not isinstance(x, str) or not x.strip() for x in plan.get("invalidation_conditions", [])):
        errors.append("explicit invalidation_conditions required")
    if not isinstance(plan.get("valuation_basis"), dict):
        errors.append("valuation_basis must be an object")
    verification = plan.get("verification") or {}
    if not isinstance(verification, dict):
        verification = {}
    for field in ("provenance", "currency", "unit"):
        if verification.get(field) is not True:
            errors.append(f"verification.{field} required")
    if plan.get("currency") != "USD":
        errors.append("currency must be USD")
    if plan.get("price_unit") not in {"per_share", "per_adr"}:
        errors.append("price_unit must be per_share/per_adr")
    if (plan.get("is_adr") or plan.get("price_unit") == "per_adr") and verification.get("adr") is not True:
        errors.append("verification.adr required for ADR")
    if plan.get("conditions_parsed") is not True:
        errors.append("entry conditions are unparsed")
    errors.extend(entry_condition_errors(plan))
    return errors


def evidence_limited_no_buy(plan):
    if not isinstance(plan,dict):return False
    entry=plan.get('entry');basis=plan.get('valuation_basis');stop=plan.get('stop')
    if not all(isinstance(value,dict) for value in (entry,basis,stop)):return False
    return (plan.get('research_intent')=='no_buy' and basis.get('kind')=='insufficient_valuation'
        and plan.get('targets')==[] and entry.get('low') is None and entry.get('high') is None
        and plan.get('take_profit')==[] and stop.get('price') is None)


def classify_plan_validation(plan) -> dict[str, list[str]]:
    """Separate a conditional valuation's entry gaps from research defects.

    This does not approve a plan or certify that a condition is satisfied.
    ``validate_plan`` remains the unchanged, strict execution contract. Only
    an independently valued thesis with explicit conditions may await a price
    band or a future fundamental event; wrong numbers and all other defects
    still require research correction and independent review.
    """
    errors = validate_plan(plan)
    entry = plan.get("entry") if isinstance(plan, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    conditions = entry.get("conditions")
    conditional = (entry.get("basis") in {"independent_valuation", "source_level"}
        and isinstance(conditions, list) and bool(conditions)
        and all(isinstance(value, str) and value.strip() for value in conditions))
    deferred = set()
    if conditional:
        if plan.get("conditions_parsed") is not True:
            deferred.add("entry conditions are unparsed")
        deferred.update(entry_condition_errors(plan))
        if entry.get("basis") == "independent_valuation":
            for field in ("low", "high"):
                if entry.get(field) is None:
                    deferred.add(f"entry.{field} must be positive")
        if (plan.get('research_intent')=='no_buy' and entry.get('low') is None and entry.get('high') is None
                and plan.get('take_profit')==[] and (plan.get('stop') or {}).get('price') is None):
            # A reviewed no-buy thesis must not invent sell rules for a purchase
            # it does not propose. Strict validate_plan still forbids execution.
            deferred.update({'take_profit schedule required','stop.price must be positive',
                             'no-buy research cannot authorize execution'})
            if evidence_limited_no_buy(plan):
                deferred.add('evidenced target scenarios required')
    return {"research_errors": [error for error in errors if error not in deferred],
            "execution_gaps": [error for error in errors if error in deferred]}


def validate_research_plan(plan) -> list[str]:
    """Research-only validation; never substitute this for execution checks."""
    return classify_plan_validation(plan)["research_errors"]
