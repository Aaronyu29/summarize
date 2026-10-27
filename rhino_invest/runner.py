"""Independent, restart-safe minute worker; no broker order execution."""
from __future__ import annotations

import signal
import threading

from .common import utc_now, parse_time, stable_id, decimal_value
from .ledger import portfolio
from .market import MarketData, quote_gate
from .research import ingest_archives, ingest_holdings, review_plan, _logical_fundamentals
from .model import ModelGateway
from .research_attention import notify_attention
from .budget import budget_status
from .decisions import evaluate, _has_provenance, valid_thesis_events
from .paper import paper_account, simulate
from .watch import evaluate_levels, KINDS as WATCH_KINDS
from .feishu import Feishu
from .reporting import (make_report, report_row, plan_row, portfolio_rows, readable,
                        reported_holdings, with_reported_holdings, notification_text, actionable_notice, beijing_time, broker_account_fields)


SAFE_ERRORS = {"ValueError", "TypeError", "KeyError", "OSError", "FileNotFoundError", "PermissionError",
               "TimeoutError", "ConnectionError", "UnknownModelOutcome", "ModelBudgetExceeded", "ModelOutputInvalid", "ModelRejected", "FeishuError",
               "UnknownOutcome", "RuntimeError", "OperationalError", "JSONDecodeError", "ImportError", "ModuleNotFoundError", "ModelBudgetConfigError"}


def _safe_error(value):
    return value if isinstance(value, str) and value in SAFE_ERRORS else "ComponentError"


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in ("as_of", "timestamp", "quote_time", "fetched_at", "ingested_at", "updated_at")}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


class TradingCalendar:
    """NYSE exchange sessions include holidays and early closes, with no clock guess."""
    def __init__(self, calendar=None):
        self.calendar = calendar

    def state(self, now):
        if self.calendar is None:
            import exchange_calendars
            self.calendar = exchange_calendars.get_calendar("XNYS")
        cal = self.calendar
        moment = parse_time(now)
        session = cal.date_to_session(moment.date().isoformat(), direction="previous")
        close = cal.session_close(session).to_pydatetime()
        opening = cal.session_open(session).to_pydatetime()
        if close > moment:
            completed = cal.previous_session(session)
        else:
            completed = session
        following = cal.next_session(completed)
        completed_close = cal.session_close(completed).to_pydatetime()
        following_close = cal.session_close(following).to_pydatetime()
        # If today has already closed, use the next actual exchange session.
        next_close = close if close > moment else cal.session_close(cal.next_session(session)).to_pydatetime()
        day, next_day = completed.date(), following.date()
        return {"source": "exchange_calendars:XNYS", "completed_session": day.isoformat(),
                "regular_open": session.date() == moment.date() and opening <= moment < close,
                "completed_close": completed_close.isoformat(), "next_regular_close": next_close.isoformat(),
                "following_close": following_close.isoformat(),
                "weekly_due": day.isocalendar()[:2] != next_day.isocalendar()[:2],
                "week": f"{day.isocalendar().year}-W{day.isocalendar().week:02}",
                "quarter": f"{day.year}-Q{(day.month - 1) // 3 + 1}",
                "quarter_end_due": (day.year, (day.month - 1) // 3) != (next_day.year, (next_day.month - 1) // 3)}


class Runner:
    def __init__(self, store, config, *, market=None, feishu=None, calendar=None,
                 model=None, primary_enricher=None, execute=False, clock=utc_now):
        self.store, self.config, self.execute, self.clock = store, config, bool(execute), clock
        self.market = market or MarketData()
        # An injected market is a complete test/custom adapter and must not
        # silently add network traffic. The production adapter also gathers
        # issuer evidence; callers can explicitly inject the same capability.
        if primary_enricher is None and market is None:
            from .primary_evidence import enrich_primary_fundamentals
            primary_enricher = enrich_primary_fundamentals
        self.primary_enricher = primary_enricher
        self.feishu = feishu or Feishu(store, config, execute=execute)
        self.calendar = calendar or TradingCalendar()
        self.model = model
        self._background_mode = False
        self._research_thread = None
        self._stop = threading.Event()
        if model is None and self.execute and config.model.get("enabled"):
            self.model = ModelGateway(store, config.model)

    def _error(self, result, stage, exc):
        # Transport exceptions may include URLs or credentials: persist type only.
        result["errors"].append({"stage": stage, "error": type(exc).__name__})

    def _plans(self):
        inventories = {account: portfolio(self.store, account, now=self.clock()).get("positions", [])
                       for account in ("real", "paper")}
        held = {p["symbol"] for account in ("real", "paper")
                for p in inventories[account]
                if decimal_value(p.get("quantity") or 0) > 0}
        recorded = {p["symbol"] for p in inventories["real"]}
        held.update(p["symbol"] for p in reported_holdings(self.store, now=self.clock())
                    if p["symbol"] not in recorded and decimal_value(p.get("reported_market_value") or 0) > 0)
        risks = {r.get("plan_id") for _, r in self.store.items("risk_evidence") if r.get("verified") is True}
        return [p for _, p in self.store.items("plan") if p.get("status") not in ("superseded", "invalidated")
                or p.get("symbol") in held or p.get("id") in risks]

    def _queue_review(self, key, payload):
        """An already observed event retains its processed receipt across ticks."""
        with self.store.transaction():
            if self.store.get("review_trigger", key) is None:
                self.store.put("review_trigger", key, dict(payload, id=key, processed=False))

    def _queue_advice_review(self, plan, advice, quote, snapshot, now):
        """Unapproved drafts are not new facts. External events survive revisions."""
        reasons = set(r for r in advice.get("reasons", []) if isinstance(r, str))
        events = []
        if "scheduled_thesis_review_due" in reasons:
            events.append(("scheduled_review", plan.get("next_review_at")))
        if "investment_horizon_expired_requires_new_thesis" in reasons:
            position = next((p for p in snapshot.get("positions", []) if p.get("symbol") == plan.get("symbol")), {})
            events.append(("holding_horizon", [position.get("first_fill_at"), plan.get("horizon_months"),
                                               plan.get("thesis_valid_from"), plan.get("horizon_until")]))
        # A sealed thesis risk already completed assessment + independent review.
        # Feeding its own recommendation back into research would manufacture
        # new risk receipts and spend again without any new external fact.
        if "confirmed_price_stop" in reasons:
            close = quote.get("regular_close") or {}
            # One review per stop condition / actual completed session. Minute
            # quote timestamps and a model's new revision cannot rearm it.
            events.append(("confirmed_stop", [plan.get("stop"), close.get("session_date")]))
        if "take_profit_schedule_reached" in reasons and advice.get("status") == "actionable":
            intent = self.store.get("advice_intent", advice.get("id", "")) or {}
            basis = advice.get("take_profit_position_basis") or {}
            events.append(("take_profit", [snapshot.get("account"), intent.get("tranche"), intent.get("baseline_quantity"),
                intent.get("id"), basis.get("first_fill_at"), basis.get("structure_hash")]))
        if "position_structure_changed_requires_take_profit_review" in reasons:
            basis = advice.get("take_profit_position_basis") or {}
            if basis.get("structure_hash"):
                events.append(("position_structure_change", {key: basis.get(key)
                    for key in ("account", "symbol", "first_fill_at", "structure_hash")}))
        for kind, evidence_identity in events:
            key = stable_id("review-fact", plan["id"], kind, evidence_identity)
            self._queue_review(key, {"symbol": plan["symbol"], "kind": kind, "timestamp": now,
                "plan_id": plan["id"], "evidence": advice})

    def _consume_legacy_review(self, key, trigger):
        """Upgrade old queued drafts without deleting their audit or paying again."""
        reason = None
        kind = trigger.get("kind")
        evidence = trigger.get("evidence") or {}
        reasons = {r for r in evidence.get("reasons", []) if isinstance(r, str)} if isinstance(evidence, dict) else set()
        external = {"confirmed_price_stop", "scheduled_thesis_review_due", "investment_horizon_expired_requires_new_thesis",
                    "take_profit_schedule_reached", "position_structure_changed_requires_take_profit_review"}
        if kind == "verified_thesis_risk":
            reason = "already_reviewed"
        elif kind == "risk_change" and not reasons & external:
            reason = "already_reviewed" if "verified_thesis_invalidation" in reasons else "no_external_fact"
        if reason is None or not self.execute:
            return False
        self.store.put("review_trigger", key, dict(trigger, processed=True, processed_at=self.clock(),
            status="superseded", reason=reason))
        return True

    def _risk_report(self, plan, advice, quote, snapshot, now):
        codes = sorted({r for r in advice.get("reasons", []) if isinstance(r, str)} &
                       {"confirmed_price_stop", "verified_thesis_invalidation"})
        if not codes:
            return None
        position = next((p for p in snapshot.get("positions", []) if p.get("symbol") == plan["symbol"]), {})
        note = next((p for p in snapshot.get("reported_holdings", []) if p.get("symbol") == plan["symbol"]), {}) if not position else {}
        quantity = position.get("quantity")
        if decimal_value(quantity or 0) <= 0 and decimal_value(note.get("reported_market_value") or 0) <= 0:
            return None  # Do not tell an unheld-stock viewer to sell imaginary shares.
        close = quote.get("regular_close") or {}
        facts = advice.get("risk_evidence_ids") or [plan.get("stop"), close.get("session_date")]
        fact_id = stable_id("independent-risk-notice", snapshot["account"], plan["id"], codes, facts,
                            position.get("first_fill_at"), str(quantity))
        policy = self.store.get("notification_policy", "automatic") or {}
        generation = policy.get("generation", 0)
        identity = stable_id(fact_id, "activation", generation) if generation else fact_id
        report_id = stable_id("report", "风险提示", identity, snapshot["account"])
        if self.store.get("report", report_id):
            return None
        report = make_report("风险提示", identity, snapshot, [plan], [advice], now=now)
        report["risk_fact_id"] = fact_id
        report["notification_generation"] = generation
        report["risk_info"] = {"symbol": plan["symbol"], "reasons": codes,
            "quantity": str(quantity) if quantity is not None and decimal_value(quantity) > 0 else None,
            "reported_market_value": note.get("reported_market_value"), "reported_at": note.get("reported_at"),
            "quote_executable": (advice.get("market_gate") or {}).get("eligible") is True,
            "advice_executable": advice.get("status") == "actionable",
            "ledger_current": snapshot.get("ledger_input_current") is not False,
            "price_plan_verified": _has_provenance(self.store, plan), "stop": plan.get("stop"),
            "risk_evidence_ids": advice.get("risk_evidence_ids", [])}
        report["notification_body"] = notification_text(report)
        self.store.put("report", report["id"], report, immutable=True)
        return report

    def _paper_skip(self, pending, reason, quote, now):
        key = stable_id("paper-preflight", pending["id"], reason, quote)
        previous = self.store.get("paper_preflight", key)
        if previous:
            return previous
        observations = []
        try:
            stamp = parse_time(quote.get("selected", quote).get("quote_time"))
            if stamp <= parse_time(pending.get("timestamp")) or stamp > parse_time(pending.get("expires_at")):
                observations.append("quote_not_after_signal_or_outside_validity")
        except (ValueError, TypeError):
            observations.append("unknown_quote_time")
        record = {"id": key, "recommendation_id": pending["id"], "account": "paper",
                  "status": "skipped", "reason": reason, "timestamp": now, "observations": observations}
        self.store.put("paper_preflight", key, record, immutable=True)
        self.store.append_event("paper", key, record)
        return record

    def _verified_risk_exit(self, plan, pending, now):
        refs = set(pending.get("risk_evidence_ids") or [])
        valid = {event["id"] for event in valid_thesis_events(self.store, plan, now=now)} if plan else set()
        return bool(refs and pending.get("action") in {"exit", "reduce"} and refs.issubset(valid))

    def _schedule_report(self, kind, period, snapshot, plans, recommendations, now):
        report = make_report(kind, period, snapshot, plans, recommendations, now=now)
        existing = self.store.get("report", report["id"])
        if existing:
            return existing
        self.store.put("report", report["id"], report, immutable=True)
        return report

    def run_once(self):
        now = self.clock()
        if self.execute:
            with self.store.transaction():
                previous = self.store.get("notification_policy", "automatic") or {}
                enabled = self.config.notifications_enabled
                generation = previous.get("generation", 0) + int(enabled and previous.get("enabled") is False)
                self.store.put("notification_policy", "automatic", {"enabled": enabled,
                    "generation": generation, "timestamp": now})
        result = {"timestamp": now, "started_at": now, "execute": self.execute, "errors": [], "quotes": [],
                  "recommendations": [], "paper_recommendations": [], "reports": [], "previews": []}
        # Poll human inputs first so evaluation never uses a stale known ledger.
        input_current = True
        if self.feishu.app_token:
            try:
                result["inputs"] = self.feishu.poll_inputs()
                input_current = not result["inputs"].get("errors")
            except Exception as exc:
                input_current = False
                self._error(result, "feishu_input", exc)
        # Input polling can take time. A pre-poll clock cannot describe a later
        # official quote/depth observation or a newly confirmed ledger entry.
        now = self.clock()
        cal = None
        try:
            cal = self.calendar.state(now)
            result["calendar"] = cal
        except Exception as exc:
            self._error(result, "trading_calendar", exc)
        initial = portfolio(self.store, "real", now=now)
        paper_initial = portfolio(self.store, "paper", now=now)
        plans = self._plans()
        paper_account(self.store)
        symbols = sorted(set(self.config.symbols) | {p["symbol"] for p in plans} |
                         {p["symbol"] for p in initial.get("positions", [])} |
                         {p["symbol"] for p in paper_initial.get("positions", [])} |
                         {p["symbol"] for p in reported_holdings(self.store, now=now)} |
                         {level["symbol"] for _, level in self.store.items("research_level")
                          if level.get("verified") is True and level.get("kind") in WATCH_KINDS
                          and isinstance(level.get("symbol"), str)})
        quotes = {}
        if symbols:
            try:
                for quote in self.market.snapshots(symbols, now=now):
                    symbol = quote.get("symbol") or quote.get("selected", {}).get("symbol")
                    if symbol:
                        if cal:
                            quote = dict(quote, next_regular_close=cal["next_regular_close"], calendar_source=cal["source"])
                        quotes[symbol] = quote
                        self.store.put("market_latest", symbol, quote)
                result["quotes"] = sorted(quotes)
            except Exception as exc:
                self._error(result, "official_quotes", exc)
                for symbol in symbols:
                    unavailable = {"symbol": symbol, "selected": {"symbol": symbol, "price": None,
                        "status": "unavailable", "quote_time": None}, "observed_at": now,
                        "business_gate": {"eligible": False, "reasons": ["official_quote_fetch_failed"]}}
                    quotes[symbol] = unavailable
                    self.store.put("market_latest", symbol, unavailable)
        fundamentals = {}
        period = cal["completed_session"] if cal else parse_time(now).date().isoformat()
        for symbol in symbols:
            cached = self.store.get("fundamental_cache", symbol)
            enrichment_revision = (("issuer-evidence-v2" if symbol in {"CRCL", "SOXX"} else "issuer-evidence-v1")
                if self.primary_enricher and self.execute else None)
            if self.config.system_remediation_enabled and symbol == 'SOXX' and enrichment_revision:
                enrichment_revision += '-remediation-v1'  # Refresh the issuer's dated NAV/PE/holdings once.
            if symbol=='GOOG' and enrichment_revision:
                enrichment_revision+='-class-filing-v1'
            refresh_due = False
            if cached and (cached.get("data") or {}).get("primary_refresh_after"):
                try:
                    refresh_due = parse_time(now) >= parse_time(cached["data"]["primary_refresh_after"])
                except (TypeError, ValueError):
                    refresh_due = True
            if (cached and cached.get("period") == period
                    and cached.get("enrichment_revision") == enrichment_revision and not refresh_due):
                fundamentals[symbol] = cached["data"]
                continue
            try:
                data = self.market.fundamentals(symbol, now=now)
                if self.primary_enricher and self.execute:
                    data = self.primary_enricher(self.store, symbol, data, now=now)
                    previous_data = cached.get("data", {}) if cached else {}
                    lost_facts = (bool((previous_data.get("primary_financials") or {}).get("facts"))
                        and not (data.get("primary_financials") or {}).get("facts"))
                    lost_etf = ((previous_data.get("etf_valuation") or {}).get("issuer_verified") is True
                        and (data.get("etf_valuation") or {}).get("issuer_verified") is not True)
                    if (lost_facts or lost_etf) and any(g.get("blocking") for g in data.get("primary_gaps", [])):
                        # A failed HTTP fetch is not a changed issuer fact. Keep
                        # last known dated evidence, never give it a new as-of.
                        failure = {"status": "retained_last_known_evidence", "attempted_at": now,
                                   "gaps": data.get("primary_gaps", [])}
                        for key, value in previous_data.items():
                            if ((key.startswith("primary_") and key not in {"primary_refresh_after", "primary_collection"})
                                    or key in {"etf_valuation", "reviewed_identity", "adr_conversion_verified"}):
                                data[key] = value
                        data["primary_collection"] = failure
                    from .market import normalize_fundamentals
                    data = normalize_fundamentals(data, store=self.store)
                fundamentals[symbol] = data
                previous = cached["data"] if cached else None
                changed = previous is not None and _logical_fundamentals(previous) != _logical_fundamentals(data)
                self.store.put("fundamental_cache", symbol, {"period": period, "data": data, "changed": changed,
                    "enrichment_revision": enrichment_revision})
                if changed:
                    self._queue_review(stable_id("fundamental_change", symbol, _logical_fundamentals(data)), {
                        "symbol": symbol, "kind": "fundamental_change", "timestamp": now,
                        "evidence": data})
            except Exception as exc:
                self._error(result, "fundamentals:" + symbol, exc)
        result['holding_research'] = ingest_holdings(self.store,
            model=self.model if self.execute and not self._background_mode else None,
            fundamentals=fundamentals, now=now, seed_only=self._background_mode)
        if self.config.archive_database:
            try:
                result["ingestion"] = ingest_archives(self.store, self.config.archive_database,
                    model=self.model if self.execute and not self._background_mode else None, symbols=symbols or None,
                    fundamentals=fundamentals, now=now, research_since=self.config.research_since)
            except Exception as exc:
                self._error(result, "sealed_archive", exc)
        # Quotes/depth are timestamped by their actual receipt. Fundamentals and
        # synchronous model work may also have aged them; evaluate freshness and
        # execution against the current clock, never the start of this run.
        now = self.clock()
        result["timestamp"] = now
        try:
            cal = self.calendar.state(now)
            result["calendar"] = cal
        except Exception as exc:
            cal = None
            self._error(result, "trading_calendar_after_fetch", exc)
        for symbol, quote in list(quotes.items()):
            quote = dict(quote, evaluated_at=now)
            if cal:
                quote.update(next_regular_close=cal["next_regular_close"], calendar_source=cal["source"])
                if "regular_open" in cal:
                    quote["execution_session_open"] = cal["regular_open"]
            else:
                quote.update(next_regular_close=None, execution_session_open=False, calendar_source=None)
            quote["business_gate"] = quote_gate(quote, now)
            quotes[symbol] = quote
            self.store.put("market_latest", symbol, quote)
        plans = self._plans()
        # A source-zone observation remains useful even when a long-term thesis
        # cannot yet be valued. It is never promoted to an investment instruction.
        with self.store.transaction():
            result["watch_events"] = evaluate_levels(self.store, quotes, now=now)
            for event in result["watch_events"]:
                report = {"id": stable_id("watch-report", event["id"]), "kind": "价格观察",
                    "period": event["timestamp"] + " · " + event["symbol"], "account": "视频观察", "timestamp": event["timestamp"],
                    "body": event["body"], "portfolio": {}, "recommendation_ids": [],
                    "watch_event_id": event["id"], "evidence_refs": event["evidence_refs"]}
                report["notification_body"] = notification_text(report, watch_event=event)
                self.store.put("report", report["id"], report, immutable=True)
                result["reports"].append(report)
        # Replay prior paper intents on this later observed quote before generating
        # new advice. Newly generated signals cannot fill on the triggering tick.
        result["paper"] = []
        current_plans = {p["id"]: p for p in plans}
        for _, pending in self.store.items("recommendation"):
            if pending.get("account") != "paper" or pending.get("status") != "actionable":
                continue
            if self.store.get("paper_fill", pending["id"]):
                continue
            if self.store.get("paper_cancel", pending["id"]):
                continue
            plan = current_plans.get(pending.get("plan_id"))
            intent = self.store.get("advice_intent", pending["id"])
            risk_exit = self._verified_risk_exit(plan, pending, now)
            if (not plan or (plan.get("status") != "active" and not risk_exit) or plan.get("revision") != pending.get("revision")
                    or not intent or intent.get("status") != "pending"):
                if not self.store.get("paper_cancel", pending["id"]):
                    self.store.put("paper_cancel", pending["id"], {"id": pending["id"], "timestamp": now,
                        "reason": "current_approved_plan_or_pending_intent_required"}, immutable=True)
                continue
            try:
                with self.store.transaction():
                    latest_plan = self.store.get("plan", plan["id"])
                    risk_exit = self._verified_risk_exit(latest_plan, pending, now)
                    if not latest_plan or (latest_plan.get("status") != "active" and not risk_exit) or latest_plan.get("revision") != pending.get("revision"):
                        continue
                    if not risk_exit and parse_time(now) >= parse_time(latest_plan.get("next_review_at")):
                        self.store.put("paper_cancel", pending["id"], {"id": pending["id"], "timestamp": now,
                            "reason": "plan_review_due_before_execution"}, immutable=True)
                        continue
                    preflight = evaluate(self.store, latest_plan, quotes.get(plan["symbol"], {}),
                        portfolio(self.store, "paper", quotes=quotes, now=now), now=now)
                    if preflight.get("id") != pending["id"] or preflight.get("status") != "actionable":
                        result["paper"].append(self._paper_skip(pending, "current_decision_not_actionable",
                            quotes.get(plan["symbol"], {}), now))
                        continue
                    result["paper"].append(simulate(self.store, pending, list(quotes.values()), now=now))
            except Exception as exc:
                self._error(result, "paper_simulation", exc)
        snapshots = {account: with_reported_holdings(self.store, portfolio(self.store, account, quotes=quotes, now=now))
                     for account in ("real", "paper")}
        for account, snapshot in snapshots.items():
            control = self.store.get("account_control", account) or {}
            if control.get("confirmed") and control.get("drawdown_limit") is not None:
                snapshot["drawdown_limit"] = control["drawdown_limit"]
                snapshot["drawdown_status"] = "not_evaluable"
        snapshots["real"]["ledger_input_current"] = input_current
        result["portfolio"] = snapshots["real"]
        ledger_hash = stable_id(self.store.events("ledger"))
        for plan in plans:
            quote = quotes.get(plan["symbol"], {"symbol": plan["symbol"], "selected": {"price": None, "status": "unavailable"}})
            try:
                review_due = parse_time(now) >= parse_time(plan.get("next_review_at"))
            except (ValueError, TypeError):
                review_due = True
            # Daily date is a conservative additional clock gate: holding-period
            # expiry and calendar review cannot disappear under an unchanged price.
            signature = stable_id(_stable(plan), _stable(quote), _stable(fundamentals.get(plan["symbol"])), ledger_hash, input_current,
                                  review_due, parse_time(now).date().isoformat(),
                                  self.store.get("plan_control", plan["id"]), self.store.get("account_control", "real"))
            old = self.store.get("evaluation_cursor", plan["id"])
            try:
                recommendation = evaluate(self.store, plan, quote, snapshots["real"], now=now)
                result["recommendations"].append(recommendation)
                self.store.put("evaluation_cursor", plan["id"], {"signature": signature,
                    "recommendation_id": recommendation.get("id"), "timestamp": now})
                semantic = stable_id(recommendation.get("action"), recommendation.get("status"),
                    recommendation.get("reasons"), recommendation.get("amount_usd"), recommendation.get("quantity"))
                old_semantic = old.get("semantic") if old else None
                self.store.put("evaluation_cursor", plan["id"], {"signature": signature,
                    "semantic": semantic, "recommendation_id": recommendation.get("id"), "timestamp": now})
                if semantic != old_semantic:
                    report = self._schedule_report("重大变化", stable_id(plan["id"], semantic, ledger_hash),
                        snapshots["real"], [plan], [recommendation], now)
                    result["reports"].append(report)
                self._queue_advice_review(plan, recommendation, quote, snapshots["real"], now)
                risk_report = self._risk_report(plan, recommendation, quote, snapshots["real"], now)
                if risk_report:
                    result["reports"].append(risk_report)
            except Exception as exc:
                self._error(result, "evaluate:" + plan["id"], exc)
            paper_key = "paper:" + plan["id"]
            paper_signature = stable_id(signature, _stable(snapshots["paper"]))
            try:
                advice = evaluate(self.store, plan, quote, snapshots["paper"], now=now)
                result["paper_recommendations"].append(advice)
                self._queue_advice_review(plan, advice, quote, snapshots["paper"], now)
                risk_report = self._risk_report(plan, advice, quote, snapshots["paper"], now)
                if risk_report:
                    result["reports"].append(risk_report)
                self.store.put("evaluation_cursor", paper_key, {"signature": paper_signature,
                    "recommendation_id": advice.get("id"), "timestamp": now})
            except Exception as exc:
                self._error(result, "paper_evaluate:" + plan["id"], exc)
        # A verified fact may refer to a superseded immutable plan revision.
        # Retain that risk context without reactivating its entry recommendations.
        seen_versions = {(p["id"], p.get("revision")) for p in plans}
        for _, risk in self.store.items("risk_evidence"):
            version = (risk.get("plan_id"), risk.get("revision"))
            if version in seen_versions:
                continue
            prior = self.store.get("plan_revision", stable_id(*version))
            if not prior or not valid_thesis_events(self.store, prior, now=now):
                continue
            seen_versions.add(version)
            try:
                prior = dict(prior, status="invalidated")
                for account, snapshot in snapshots.items():
                    advice = evaluate(self.store, prior, quotes.get(prior["symbol"], {}), snapshot, now=now)
                    report = self._risk_report(prior, advice, quotes.get(prior["symbol"], {}), snapshot, now)
                    if report:
                        result["reports"].append(report)
            except Exception as exc:
                self._error(result, "retained_thesis_risk", exc)
        # Reservations can change during evaluation; publish the resulting cash
        # availability rather than the pre-evaluation planning snapshot.
        for account in snapshots:
            refreshed = with_reported_holdings(self.store, portfolio(self.store, account, quotes=quotes, now=now))
            for key in ("ledger_input_current", "drawdown_limit", "drawdown_status"):
                if key in snapshots[account]:
                    refreshed[key] = snapshots[account][key]
            snapshots[account] = refreshed
        result["portfolio"] = snapshots["real"]
        if cal:
            schedule = [("每日复盘", cal["completed_session"])]
            if cal["weekly_due"]:
                schedule.append(("每周复盘", cal["week"]))
            if cal["quarter_end_due"]:
                schedule.append(("季度复核", cal["quarter"]))
            for kind, key in schedule:
                marker = stable_id(kind, key)
                if self.store.get("schedule", marker):
                    continue
                for account, snapshot in snapshots.items():
                    account_advice = result["paper_recommendations"] if account == "paper" else result["recommendations"]
                    result["reports"].append(self._schedule_report(kind, key, snapshot, plans, account_advice, now))
                self.store.put("schedule", marker, {"kind": kind, "period": key, "timestamp": now})
                if kind == "季度复核":
                    for plan in plans:
                        self._queue_review(stable_id("quarterly_review", plan["id"], key), {
                            "symbol": plan["symbol"], "kind": "quarterly_review", "timestamp": now,
                            "plan_id": plan["id"], "revision": plan.get("revision"), "evidence": {"period": key}})
        for key, trigger in self.store.items("review_trigger"):
            if trigger.get("processed") or self._consume_legacy_review(key, trigger):
                continue
            if not self.model or not self.execute or self._background_mode:
                continue
            try:
                for plan in plans:
                    if plan["symbol"] == trigger["symbol"]:
                        review_plan(self.store, plan, self.model, event_id=key, evidence=trigger["evidence"], now=now)
                self.store.put("review_trigger", key, dict(trigger, processed=True, processed_at=now))
            except Exception as exc:
                self._error(result, "review:" + key, exc)
        # Export all pending projections each tick; the Bitable outbox handles receipts.
        # Local report creation never implies an external send succeeded.
        if self.feishu.app_token or not self.execute:
            for account, snapshot in snapshots.items():
                fields = {"系统标识": account, "账户": account, "币种": "USD", "目标总资产美元": "20000",
                          "实际现金美元": readable(snapshot.get("cash")), "持仓市值美元": readable(snapshot.get("market_value")),
                          "总权益美元": readable(snapshot.get("total_equity")), "现金完整": readable(snapshot.get("cash_complete")), "持仓核对状态": "已核对" if snapshot.get("holdings_complete") else "未核对",
                          "成本完整": readable(snapshot.get("cost_complete")), "费用完整": readable(snapshot.get("fees_complete")),
                          "规划预留美元": readable(snapshot.get("planning_reserved_cash")),
                          "可用现金美元": readable(snapshot.get("available_cash_after_reservations")),
                          "回撤限制": ("未配置" if snapshot.get("drawdown_limit") is None else
                                      "已设置" + str(snapshot["drawdown_limit"]) + "；历史净值峰值不足，不能评估"), "更新时间": now}
                fields.update(broker_account_fields(snapshot))
                self._export(result, "账户与资金", fields)
                for row in portfolio_rows(snapshot):
                    self._export(result, "持仓总览", row)
            for plan in plans:
                cursor = self.store.get("evaluation_cursor", plan["id"]) or {}
                recommendation = self.store.get("recommendation", cursor.get("recommendation_id", ""))
                fields = plan_row(plan, recommendation)
                if plan.get('research_origin') == 'holding_fundamentals' and plan.get('research_preflight'):
                    fields['理由'] = '；'.join(item['detail'] for item in plan['research_preflight'][:3])
                    pending = self.store.get('holding_research_state', plan['symbol']) or {}
                    wait = {'ModelBudgetExceeded': '研究审核额度暂不足，等待额度恢复',
                        'UnknownModelOutcome': '上次模型审核结果尚未核实，暂停重复请求'}.get(pending.get('error'))
                    if wait:
                        fields['理由'] = wait + '；' + fields['理由']
                self._export(result, "投资计划", fields)
            for key, report in self.store.items("report"):
                self._export(result, "复盘报告", report_row(report))
        report_items = self.store.items("report")
        scheduled_kinds = {"每日复盘", "每周复盘", "季度复核"}
        latest_scheduled = {}
        for key, report in report_items:
            if report.get("kind") in scheduled_kinds:
                group = (report.get("kind"), report.get("account", "real"))
                order = (parse_time(report["timestamp"]), key)
                if group not in latest_scheduled or order > latest_scheduled[group][0]:
                    latest_scheduled[group] = (order, key)
        for key, report in report_items:
            if not self.store.get("notification", key):
                try:
                    if not self.config.notifications_enabled:
                        if self.execute:
                            self.store.put("notification", key, {"report_id": key, "status": "suppressed",
                                "reason": "notifications_disabled", "timestamp": now}, immutable=True)
                        continue
                    if report.get("kind") == "风险提示" and report.get("risk_fact_id"):
                        # A new activation is not permission to bypass an earlier
                        # uncertain send for the same risk fact.
                        related = {"notice:" + old_id for old_id, old in report_items
                                   if old_id != key and old.get("kind") == "风险提示"
                                   and (old.get("risk_fact_id") or old.get("period")) == report["risk_fact_id"]}
                        if any(op.get("identity") in related and op.get("status") in {"pending", "unknown"}
                               for _, op in self.store.items("feishu_outbox")):
                            if self.execute:
                                self.store.put("notification", key, {"report_id": key, "status": "suppressed",
                                    "reason": "prior_risk_notice_outcome_unknown", "timestamp": now}, immutable=True)
                            continue
                    group = (report.get("kind"), report.get("account", "real"))
                    if report.get("kind") in scheduled_kinds and latest_scheduled[group][1] != key:
                        if self.execute:
                            self.store.put("notification", key, {"report_id": key, "status": "suppressed",
                                "reason": "newer_scheduled_report_available", "timestamp": now}, immutable=True)
                        continue
                    advice = [self.store.get("recommendation", identity) for identity in report.get("recommendation_ids", [])]
                    advice = [rec for rec in advice if rec]
                    delivery_plans = self._plans()
                    dedicated_risk = any({r for r in rec.get("reasons", []) if isinstance(r, str)} &
                                         {"confirmed_price_stop", "verified_thesis_invalidation"} for rec in advice)
                    if report.get("kind") == "重大变化" and (dedicated_risk or not any(actionable_notice(rec, delivery_plans, now=now) for rec in advice)):
                        # Draft validation and routine waiting states stay in the
                        # audit DB and the daily summary, without one alert per stock.
                        if self.execute:
                            self.store.put("notification", key, {"report_id": key, "status": "suppressed",
                                "reason": "no_current_actionable_advice", "timestamp": now}, immutable=True)
                        continue
                    watch = self.store.get("watch_event", report.get("watch_event_id", "")) if report.get("kind") == "价格观察" else None
                    app_meta = self.store.get("feishu_meta", "app") or {}
                    text = notification_text(report, current_plans=delivery_plans, recommendations=advice, watch_event=watch,
                                             now=now, table_url=app_meta.get("app_url"))
                    receipt = self.feishu.notify(key, text)
                    if self.execute and receipt.get("status") != "disabled":
                        self.store.put("notification", key, {"report_id": key, "receipt": receipt, "timestamp": now}, immutable=True)
                    elif not self.execute:
                        result["previews"].append(receipt)
                except Exception as exc:
                    self._error(result, "notification:" + key, exc)
        try:
            result['research_attention'] = notify_attention(self.store, self.feishu, plans,
                now=now, when=beijing_time(now), enabled=self.config.notifications_enabled, execute=self.execute)
        except Exception as exc:
            self._error(result, 'research_attention', exc)
        self.store.put("worker", "latest", {"timestamp": now, "execute": self.execute,
            "notifications_enabled": self.config.notifications_enabled,
            "model_budget": budget_status(self.store, self.config.model, now=self.clock()),
            "quote_count": len(result["quotes"]), "errors": result["errors"], "report_count": len(result["reports"]),
            "recommendation_count": len(result["recommendations"])})
        self._observe_health(result, now)
        if self._background_mode and self.execute and self.model and not self._stop.is_set():
            if not self._research_thread or not self._research_thread.is_alive():
                self._research_thread = threading.Thread(target=self._research_cycle,
                    args=(symbols, fundamentals), name="investment-research", daemon=False)
                self._research_thread.start()
            result["research"] = "background_worker"
        return result

    def _research_cycle(self, symbols, fundamentals):
        owner = self
        class CancellableModel:
            config = getattr(owner.model, "config", {})
            def run(self, *args, **kwargs):
                if owner._stop.is_set():
                    raise RuntimeError("Investment worker is stopping; no new paid call started")
                return owner.model.run(*args, **kwargs)
        model = CancellableModel()
        summary = {"timestamp": self.clock(), "status": "completed", "errors": [], "deferred": [],
                   "ingestion": {"ingested": 0, "unchanged": 0, "plan_count": 0, "deferred_before_since_count": 0,
                                 "deferred_unresolved_risk_count": 0},
                   "reviewed_count": 0}
        try:
            if not self._stop.is_set():
                holdings = ingest_holdings(self.store, model=model, fundamentals=fundamentals, now=self.clock())
                summary['holding_research'] = {'symbols': holdings['symbols'], 'plan_count': len(holdings['plans']),
                    'needs_evidence_count': sum(plan.get('status') != 'active' for plan in holdings['plans']),
                    'deferred_unresolved_risk_count': len(holdings.get('deferred_unresolved_risk', []))}
                if holdings.get('deferred_unresolved_risk'):
                    summary['deferred'].append({'stage': 'holding_verified_thesis_risk',
                        'reason': 'UNRESOLVED_VERIFIED_THESIS_RISK'})
                for error in holdings['errors']:
                    item = {'stage': 'holding_research', 'error': _safe_error(error.get('error'))}
                    summary['deferred' if item['error'] == 'ModelBudgetExceeded' else 'errors'].append(item)
            if self.config.archive_database and not self._stop.is_set():
                ingestion = ingest_archives(self.store, self.config.archive_database, model=model,
                    symbols=symbols or None, fundamentals=fundamentals, now=self.clock(), research_since=self.config.research_since)
                summary["ingestion"] = {"ingested": ingestion.get("ingested", 0), "unchanged": ingestion.get("unchanged", 0),
                    "plan_count": len(ingestion.get("plans", [])),
                    "deferred_before_since_count": len(ingestion.get("deferred_before_since", [])),
                    "deferred_unresolved_risk_count": len(ingestion.get("deferred_unresolved_risk", []))}
                if ingestion.get("deferred_unresolved_risk"):
                    summary["deferred"].append({"stage": "verified_thesis_risk", "reason": "UNRESOLVED_VERIFIED_THESIS_RISK"})
                for error in ingestion.get("errors", []):
                    item = {"stage": "archive_ingestion", "error": _safe_error(error.get("error"))}
                    summary["deferred" if item["error"] == "ModelBudgetExceeded" else "errors"].append(item)
            if getattr(self.config, 'system_remediation_enabled', False) and not self._stop.is_set():
                from .remediation import remediate_plans, adjudicate_research
                repair = remediate_plans(self.store, model, fundamentals, self.clock(), self.config.symbols)
                audit=adjudicate_research(self.store,model,fundamentals,self.clock(),self.config.symbols)
                repair['research_execution_reaudit']=audit
                repair['errors'].extend(audit['errors']);repair['deferred'].extend(audit['deferred'])
                summary['system_remediation'] = repair
                summary['errors'].extend({'stage':'system_remediation', 'error':_safe_error(item.get('error'))}
                    for item in repair['errors'])
                summary['deferred'].extend({'stage':'system_remediation', **item} for item in repair['deferred'])
            for key, trigger in self.store.items("review_trigger"):
                if trigger.get("processed") or self._stop.is_set():
                    continue
                if self._consume_legacy_review(key, trigger):
                    continue
                for plan in self._plans():
                    if plan["symbol"] == trigger["symbol"]:
                        review_plan(self.store, plan, model, event_id=key, evidence=trigger["evidence"], now=self.clock())
                        summary["reviewed_count"] += 1
                self.store.put("review_trigger", key, dict(trigger, processed=True, processed_at=self.clock()))
            from .remediation import update_research_followups
            update_research_followups(self.store,self.clock())
            summary.update(timestamp=self.clock(), error_count=len(summary["errors"]), deferred_count=len(summary["deferred"]),
                           status="blocked" if summary["errors"] else "deferred" if summary["deferred"] else "completed")
            self.store.put("research_worker", "latest", summary)
        except Exception as exc:
            try:
                code = _safe_error(type(exc).__name__)
                summary["deferred" if code == "ModelBudgetExceeded" else "errors"].append({"stage": "research_worker", "error": code})
                summary.update(timestamp=self.clock(), error_count=len(summary["errors"]), deferred_count=len(summary["deferred"]),
                               status="blocked" if summary["errors"] else "deferred")
                self.store.put("research_worker", "latest", summary)
            except Exception:
                pass  # Process shutdown retains pre-call outbox/model fences.

    def _observe_health(self, result, now):
        """One operational notice per durable health transition, never advice."""
        try:
            issues = {(str(e.get("stage", "worker")).split(":", 1)[0], _safe_error(e.get("error")))
                      for e in result.get("errors", [])}
            for error in result.get("ingestion", {}).get("errors", []):
                issues.add(("archive_ingestion", _safe_error(error.get("error"))))
            research = self.store.get("research_worker", "latest") or {}
            research_state = research.get("status")
            if research_state == "blocked":
                issues.update(("research", _safe_error(e.get("error"))) for e in research.get("errors", []))
                if not research.get("errors"):
                    issues.add(("research", _safe_error(research.get("error"))))
            unresolved_risk = bool((research.get("ingestion") or {}).get("deferred_unresolved_risk_count") or
                                   (research.get("holding_research") or {}).get("deferred_unresolved_risk_count") or
                                   result.get("ingestion", {}).get("deferred_unresolved_risk") or
                                   result.get("holding_research", {}).get("deferred_unresolved_risk"))
            deferred = research_state == "deferred" or unresolved_risk or any(code == "ModelBudgetExceeded" for _, code in issues)
            failures = sorted((stage, code) for stage, code in issues if code != "ModelBudgetExceeded")
            state = "blocked" if failures else "deferred" if deferred else "healthy"
            # Deduplicate user-visible impact, not changing exception names or
            # alternating pipeline stages that all mean research is paused.
            categories = {"research": "research_paused", "archive_ingestion": "research_paused",
                          "sealed_archive": "research_paused", "review": "research_paused",
                          "retained_thesis_risk": "research_paused"}
            impacts = {categories.get(stage, stage) for stage, _ in failures}
            for _,code in failures:
                if code in {'UnknownModelOutcome','UnknownOutcome','ModelBudgetConfigError'}:impacts.add(code)
            if deferred:
                impacts.add("research_paused")
            if unresolved_risk:impacts.add('verified_thesis_risk')
            signature = stable_id("operations-impact", sorted(impacts))
            previous = self.store.get("ops_health", "latest")
            if not self.execute:
                return
            if previous and not self.config.notifications_enabled:
                previous = dict(previous, notice_attempted=False)
                self.store.put("ops_health", "latest", previous)
            observation = self.store.get("ops_observation", "latest") or {}
            consecutive = observation.get("consecutive", 0) + 1 if observation.get("signature") == signature else 1
            self.store.put("ops_observation", "latest", {"signature": signature, "consecutive": consecutive,
                "status": state, "timestamp": now})
            if previous and previous.get("signature") == signature:
                self.store.put("ops_health", "latest", dict(previous, status=state, timestamp=now,
                    issues=[{"stage": a, "error": b} for a, b in failures]))
                return
            urgent = research_state == "blocked" or any(code in {"UnknownModelOutcome", "UnknownOutcome"} for _, code in failures)
            if state == "blocked" and not urgent and consecutive < 2:
                return  # One transient poll/transport error stays in the audit log.
            sequence = (previous or {}).get("sequence", 0) + 1
            transition = {"signature": signature, "status": state, "sequence": sequence,
                          "timestamp": now, "issues": [{"stage": a, "error": b} for a, b in failures],
                          "notice_attempted": False}
            # Save before attempting delivery: transport uncertainty must not cause
            # a new operational notice on every subsequent quote tick.
            self.store.put("ops_health", "latest", transition)
            if previous is None and state == "healthy":
                return
            key = stable_id("investment-operations", sequence, signature)
            if impacts=={'research_paused'}:
                self.store.put('ops_notice',key,{'id':key,'status':'suppressed','health':state,
                    'timestamp':now,'reason':'internal_research_progress'})
                return  # Research revisions/budget waiting are not user-facing incidents.
            if not self.config.notifications_enabled:
                self.store.put("ops_notice", key, {"id": key, "status": "suppressed", "health": state,
                    "timestamp": now, "reason": "notifications_disabled"})
                return
            if state == "healthy" and previous and previous.get("notice_attempted") is False:
                return  # No recovery alert for an incident the user never received.
            title = "运行异常" if state == "blocked" else "研究延后" if state == "deferred" else "运行已恢复"
            lines = ["【犀牛投资·运维通知】" + title + " · " + beijing_time(now)]
            if failures:
                effects = {"research": "研究待核查，新研究建议暂缓", "archive_ingestion": "研究待核查，新研究建议暂缓",
                           "official_quotes": "行情暂不可用，相关操作建议暂缓", "feishu_input": "账本暂无法核对，新增操作建议暂缓",
                           "notification": "部分报告通知延后", "research_attention": "研究审核结果通知待核实", "export": "工作台更新暂时延后"}
                impact = list(dict.fromkeys(effects.get(stage, "投资任务暂不可用") for stage, _ in failures))
                lines.append("影响：" + "；".join(impact[:2]) + "。")
                codes = {code for _, code in failures}
                reasons = {"ModelOutputInvalid": "模型已返回，但输出格式校验未通过，相关研究尚未完成；原回执已保留，恢复状态需核查",
                           "UnknownModelOutcome": "模型请求结果不明，需核对供应商回执，已禁止重复付费请求",
                           "UnknownOutcome": "远端写入结果不明，需核对回执，已禁止重复写入",
                           "ModelRejected": "模型接口已明确拒绝请求，需检查权限、额度或配置",
                           "ModelBudgetConfigError": "模型预算配置无效，已暂停新付费请求",
                           "ConnectionError": "远端服务连接失败，等待后续检查恢复",
                           "TimeoutError": "远端请求超时，等待后续检查恢复"}
                lines.append("原因：" + "；".join(dict.fromkeys(reasons.get(code, "组件检查未通过，需核查对应任务记录") for code in sorted(codes))) + "。")
                lines.append("处理：保留证据、账本和检查点；系统继续检查，无需重复提交。非投资建议。")
            elif deferred:
                lines.append("影响：已核实风险尚未解除，相关新计划暂缓。" if unresolved_risk else "影响：新研究暂时延后，现有账本保留。")
                lines.append("处理：等待专门复核风险，无需重复提交。非投资建议。" if unresolved_risk else
                             "处理：等待预算或条件恢复，无需重复提交。非投资建议。")
            else:
                lines.append("此前异常已恢复，系统继续跟踪；无需重复操作。非投资建议。")
            self.store.put("ops_notice", key, {"id": key, "status": "pending", "health": state, "timestamp": now})
            self.store.put("ops_health", "latest", dict(transition, notice_attempted=True))
            try:
                receipt = self.feishu.notify(key, "\n".join(lines))
                self.store.put("ops_notice", key, {"id": key, "status": "disabled" if receipt.get("status") == "disabled" else "acknowledged",
                    "health": state, "timestamp": now, "receipt": receipt})
            except Exception as exc:
                self.store.put("ops_notice", key, {"id": key, "status": "unknown", "health": state,
                    "timestamp": now, "error": _safe_error(type(exc).__name__)})
                # Deliberately do not append this failure to worker errors or
                # notify again: notification errors cannot recurse into alerts.
        except Exception:
            pass  # Operational reporting cannot take down the investment worker.

    def _export(self, result, table, fields):
        try:
            identity = stable_id(table, fields["系统标识"])
            signature = stable_id({k: v for k, v in fields.items() if k != "更新时间"})
            previous = self.store.get("feishu_projection", identity)
            if self.execute and previous and previous.get("signature") == signature:
                return
            receipt = self.feishu.upsert(table, fields)
            if not self.execute:
                result["previews"].append(receipt)
            else:
                self.store.put("feishu_projection", identity, {"signature": signature, "timestamp": self.clock()})
        except Exception as exc:
            self._error(result, "export:" + table, exc)

    def serve(self, *, stop=None):
        stop = stop or threading.Event()
        self._stop = stop
        self._background_mode = True
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                failed = {"timestamp": self.clock(), "execute": self.execute,
                    "errors": [{"stage": "worker", "error": type(exc).__name__}]}
                self.store.put("worker", "latest", failed)
                self._observe_health(failed, failed["timestamp"])
            stop.wait(self.config.interval_seconds)
        if self._research_thread and self._research_thread.is_alive():
            self.store.put("worker_shutdown", "latest", {"timestamp": self.clock(),
                "status": "draining", "research_inflight": True, "grace_seconds": 660})
            self._research_thread.join(timeout=660)
            if self._research_thread.is_alive():
                # Persist uncertainty before the service supervisor's longer
                # stop deadline. Do not abandon/interrupt the paid request here;
                # its pre-call model fence remains authoritative across restart.
                self.store.put("worker_shutdown", "latest", {"timestamp": self.clock(),
                    "status": "inflight_outcome_unknown", "research_inflight": True,
                    "grace_seconds": 660, "waiting_for_receipt": True})
                self._research_thread.join()
            self.store.put("worker_shutdown", "latest", {"timestamp": self.clock(),
                "status": "completed", "research_inflight": False})


def status(store):
    outbox = [v for _, v in store.items("feishu_outbox")]
    return {"worker": store.get("worker", "latest"),
            "research_worker": store.get("research_worker", "latest"),
            "shutdown": store.get("worker_shutdown", "latest"),
            "portfolio": with_reported_holdings(store, portfolio(store, "real")), "paper_portfolio": portfolio(store, "paper"),
            "staged_count": len(store.items("ledger_staged")),
            "confirmed_count": len(store.events("ledger")), "plan_count": len(store.items("plan")),
            "pending_feishu": sum(v["status"] in ("pending", "unknown") for v in outbox),
            "model_calls": len(store.items("model_call")), "model_receipts": len(store.items("model_receipt"))}
