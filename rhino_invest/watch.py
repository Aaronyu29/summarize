"""Video price-zone observations, independent of investment plans or trades."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from .common import decimal_value, parse_time, stable_id, utc_now
from .ledger import _effective


KINDS = {"technical_support", "creator_entry", "stop", "target"}
LABELS = {"technical_support": "技术支撑", "creator_entry": "博主条件入场区间",
          "stop": "视频止损观察位", "target": "视频目标观察位"}
SESSIONS = {"regular": "常规时段", "premarket": "盘前", "pre_market": "盘前",
            "postmarket": "盘后", "post_market": "盘后", "after_hours": "盘后", "overnight": "夜盘"}


def _publication(snapshot):
    metadata = snapshot.get("metadata") or {}
    timestamp = metadata.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, timezone.utc)
    date = metadata.get("upload_date")
    if isinstance(date, str) and re.fullmatch(r"\d{8}", date):
        return datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
    raise ValueError("source publication time unknown")


def _source(store, level, seals, extracted):
    """Bind a verification result to its persisted extraction and archive seal."""
    refs = level.get("evidence_refs")
    if not isinstance(refs, list) or not refs or stable_id(level) not in extracted:
        return None
    sources = []
    for ref in refs:
        source = store.get("research_evidence", ref)
        if not source or source.get("id") != ref:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(source.get("video_id", ""))):
            return None
        expected = (ref, source.get("analysis_sha256"), source.get("manifest_sha256"))
        if expected not in seals or not all(re.fullmatch(r"[0-9a-f]{64}", str(v or "")) for v in expected[1:]):
            return None
        content = {k: v for k, v in source.items() if k not in {"id", "ingested_at"}}
        if stable_id("research-evidence-v1", content) != ref:
            return None
        try:
            published = _publication(source)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
        sources.append((published, source))
    return max(sources, key=lambda item: item[0])


def _eligible_level(level, source):
    if level.get("verified") is not True or level.get("issues") or level.get("kind") not in KINDS:
        return False
    if level.get("currency") != "USD" or level.get("price_unit") not in {"per_share", "per_adr"}:
        return False
    for key in ("source_date", "source_horizon", "adjustment", "conditions"):
        if level.get(key) is None:
            return False
    if not str(level.get("adjustment", "")).strip():
        return False
    segments = {s.get("id") for s in (source.get("transcript") or {}).get("segments", [])}
    refs = level.get("evidence_segment_ids") or []
    frames = {frame.get("timestamp_seconds") for frame in source.get("frames", [])}
    if not refs or any(ref not in segments for ref in refs) or level.get("frame_seconds") not in frames:
        return False
    try:
        offset = decimal_value(level.get("timestamp_seconds"))
        if offset < 0 or offset > decimal_value((source.get("metadata") or {}).get("duration")):
            return False
    except ValueError:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", str(source.get("video_id", ""))))


def _zone(level):
    if level.get("low") is not None and level.get("high") is not None:
        low, high = decimal_value(level["low"]), decimal_value(level["high"])
    elif level.get("value") is not None:
        low = high = decimal_value(level["value"])
    else:
        raise ValueError("complete source price zone required")
    if low <= 0 or high < low:
        raise ValueError("invalid source price zone")
    return low, high


def _quote(snapshot, now, adjustment):
    q = snapshot.get("selected", snapshot)
    if (q.get("source") != "longbridge-openapi" or q.get("status") != "current"
            or q.get("currency") != "USD" or q.get("session") not in SESSIONS
            or q.get("permission_verified", snapshot.get("permission_verified")) is not True
            or q.get("delayed") not in (None, False, 0)):
        return None
    try:
        timestamp = parse_time(q.get("quote_time"))
        age = (now - timestamp).total_seconds()
        price = decimal_value(q.get("price"))
        if price <= 0 or not 0 <= age <= (30 if q["session"] == "regular" else 120):
            return None
    except (ValueError, TypeError):
        return None
    # A current spot quote has no historical adjustment. An adjusted source
    # requires an explicitly matching quote basis; never guess a split ratio.
    raw_basis = str(adjustment).lower().replace("_", "").replace(" ", "")
    if raw_basis not in {"noadjust", "none", "unadjusted", "不复权"} and q.get("adjustment") != adjustment:
        return None
    return q, timestamp, price


def _revision(value):
    text = str(value)
    return (1, int(text)) if text.isdigit() else (0, text)


def evaluate_levels(store, snapshots, now=None):
    """Emit durable watch events on a zone entry or an observed cross-zone gap.

    No active long-term plan is required. Valuation and historical quotations
    never participate. Only the newest source publication for each symbol is
    watched, including when newer evidence is still unverified.
    """
    moment = parse_time(now or utc_now())
    observed_at = moment.isoformat()
    if isinstance(snapshots, dict):
        snapshots = [snapshots] if "selected" in snapshots or "symbol" in snapshots else list(snapshots.values())
    quotes = {s.get("symbol") or s.get("selected", {}).get("symbol"): s for s in snapshots
              if isinstance(s, dict) and isinstance(s.get("selected", s), dict)}
    with store.transaction():
        # A confirmed real-account split invalidates older absolute price zones.
        # Read the effective ledger so a void/correction cannot leave a phantom
        # corporate action. Never infer a new zone by dividing creator numbers.
        real_events = [e for e in store.events("ledger") if e.get("account") == "real"
                       and parse_time(e.get("timestamp")) <= moment]
        splits = {}
        for event in _effective(real_events, "real"):
            if event["type"] == "split" and parse_time(event["timestamp"]) <= moment:
                stamp = parse_time(event["timestamp"])
                splits[event["symbol"]] = max(splits.get(event["symbol"], stamp), stamp)
        seals = {(s.get("snapshot_id"), s.get("analysis_sha256"), s.get("manifest_sha256"))
                 for _, s in store.items("research_archive_seal")}
        extracted = {stable_id(level) for _, result in store.items("research_extraction")
                     for level in result.get("levels", []) if isinstance(level, dict)}
        candidates, newest, revisions = [], {}, {}
        for key, level in store.items("research_level"):
            symbol = level.get("symbol")
            if key != level.get("id") or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or symbol.endswith(".US"):
                continue
            bound = _source(store, level, seals, extracted)
            if bound is None:
                continue
            published, source = bound
            if published > moment:
                continue
            newest[symbol] = max(newest.get(symbol, published), published)
            revision_key = (symbol, published, source["video_id"])
            revisions[revision_key] = max(revisions.get(revision_key, _revision(source.get("revision"))), _revision(source.get("revision")))
            candidates.append((key, level, published, source))
        events = []
        for key, level, published, source in candidates:
            symbol = level["symbol"]
            if (published != newest[symbol] or _revision(source.get("revision")) != revisions[(symbol, published, source["video_id"])]
                    or not _eligible_level(level, source) or symbol not in quotes
                    or (symbol in splits and splits[symbol] > published)):
                continue
            try:
                low, high = _zone(level)
            except ValueError:
                continue
            current = _quote(quotes[symbol], moment, level["adjustment"])
            if current is None:
                continue
            quote, quote_time, price = current
            if quote.get("symbol", symbol) != symbol:
                continue
            previous = store.get("watch_state", key)
            if previous and (quote_time <= parse_time(previous["quote_time"]) or moment < parse_time(previous["observed_at"])):
                continue
            side = "below" if price < low else "above" if price > high else "inside"
            trigger = None
            if side == "inside" and (not previous or previous.get("side") != "inside"):
                trigger = "zone_enter"
            elif previous and {side, previous.get("side")} == {"below", "above"}:
                trigger = "gap_cross"
            state = {"id": key, "level_id": level["id"], "symbol": symbol, "price": str(price),
                     "quote_time": quote_time.isoformat(), "observed_at": observed_at,
                     "side": side, "source_published_at": published.isoformat(), "session": quote["session"]}
            store.put("watch_state", key, state)
            if trigger is None:
                continue
            event_id = stable_id("video-zone-watch-v1", key, trigger, quote_time.isoformat(), observed_at, str(price))
            offset = decimal_value(level["timestamp_seconds"])
            minutes, seconds = divmod(int(offset), 60)
            warning = "报价跨越视频观察区间，不构成买入建议（未观察到实际触达）" if trigger == "gap_cross" else "触达视频观察区间，不构成买入建议"
            cross = "两个有效报价跨越观察区间；未观察到区间内报价，不能视为实际触达。" if trigger == "gap_cross" else "已观察到区间内报价；条件是否满足仍须独立核实。"
            body = (f"{symbol}｜{warning}\n{cross}\n"
                    f"视频{LABELS[level['kind']]}：${low}–${high}；实际报价：${price}（{SESSIONS[quote['session']]}，USD）\n"
                    f"行情时间：{quote_time.isoformat()}；来源：longbridge-openapi\n"
                    f"视频发布：{published.isoformat()}；画面/原话时间：{minutes:02}:{seconds:02}\n"
                    f"原条件：{level['conditions']}；原口径：{level['price_unit']}，{level['adjustment']}；原期限：{level['source_horizon']}\n"
                    f"视频：https://www.youtube.com/watch?v={source['video_id']}&t={int(offset)}\n"
                    f"证据：{', '.join(level['evidence_refs'])}；level={level['id']}")
            event = {"id": event_id, "timestamp": observed_at, "now": observed_at, "symbol": symbol,
                     "kind": "video_level_watch", "trigger": trigger, "body": body,
                     "touch_observed": trigger == "zone_enter", "trade_or_recommendation": False,
                     "price": str(price), "zone": {"low": str(low), "high": str(high)},
                     "quote_time": quote_time.isoformat(), "session": quote["session"], "currency": "USD",
                     "source": quote["source"], "level_id": level["id"], "level_kind": level["kind"],
                     "conditions": level["conditions"], "adjustment": level["adjustment"],
                     "price_unit": level["price_unit"], "source_horizon": level["source_horizon"],
                     "source_published_at": published.isoformat(), "video_id": source["video_id"],
                     "video_timestamp_seconds": str(offset), "evidence": level["evidence_refs"],
                     "evidence_refs": level["evidence_refs"], "evidence_segment_ids": level["evidence_segment_ids"],
                     "prior_price": previous.get("price") if previous else None}
            store.put("watch_event", event_id, event, immutable=True)
            store.append_event("watch", event_id, event)
            events.append(event)
        return events
