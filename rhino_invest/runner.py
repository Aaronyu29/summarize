"""Independent, restart-safe minute worker; no broker order execution."""
from __future__ import annotations

import signal
import threading

from .common import utc_now, parse_time, stable_id
from .ledger import portfolio
from .market import MarketData, quote_gate
from .research import ingest_archives, review_plan
from .model import ModelGateway
from .decisions import evaluate
from .paper import paper_account, simulate
from .watch import evaluate_levels, KINDS as WATCH_KINDS
from .feishu import Feishu
from .reporting import make_report, report_row, plan_row, portfolio_rows, readable


SAFE_ERRORS = {"ValueError", "TypeError", "KeyError", "OSError", "FileNotFoundError", "PermissionError",
               "TimeoutError", "ConnectionError", "UnknownModelOutcome", "ModelBudgetExceeded", "FeishuError",
               "UnknownOutcome", "RuntimeError", "OperationalError", "JSONDecodeError", "ImportError", "ModuleNotFoundError"}


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
                 model=None, execute=False, clock=utc_now):
        self.store, self.config, self.execute, self.clock = store, config, bool(execute), clock
        self.market = market or MarketData()
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
        return [p for _, p in self.store.items("plan") if p.get("status") not in ("superseded", "invalidated")]

    def _queue_review(self, key, payload):
        """An already observed event retains its processed receipt across ticks."""
        with self.store.transaction():
            if self.store.get("review_trigger", key) is None:
                self.store.put("review_trigger", key, dict(payload, id=key, processed=False))

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

    def _schedule_report(self, kind, period, snapshot, plans, recommendations, now):
        report = make_report(kind, period, snapshot, plans, recommendations, now=now)
        existing = self.store.get("report", report["id"])
        if existing:
            return existing
        self.store.put("report", report["id"], report, immutable=True)
        return report

    def run_once(self):
        now = self.clock()
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
            if cached and cached.get("period") == period:
                fundamentals[symbol] = cached["data"]
                continue
            try:
                data = self.market.fundamentals(symbol, now=now)
                fundamentals[symbol] = data
                previous = cached["data"] if cached else None
                changed = previous is not None and _stable(previous) != _stable(data)
                self.store.put("fundamental_cache", symbol, {"period": period, "data": data, "changed": changed})
                if changed:
                    self._queue_review(stable_id("fundamental_change", symbol, _stable(data)), {
                        "symbol": symbol, "kind": "fundamental_change", "timestamp": now,
                        "evidence": data})
            except Exception as exc:
                self._error(result, "fundamentals:" + symbol, exc)
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
            if (not plan or plan.get("status") != "active" or plan.get("revision") != pending.get("revision")
                    or not intent or intent.get("status") != "pending"):
                if not self.store.get("paper_cancel", pending["id"]):
                    self.store.put("paper_cancel", pending["id"], {"id": pending["id"], "timestamp": now,
                        "reason": "current_approved_plan_or_pending_intent_required"}, immutable=True)
                continue
            try:
                with self.store.transaction():
                    latest_plan = self.store.get("plan", plan["id"])
                    if not latest_plan or latest_plan.get("status") != "active" or latest_plan.get("revision") != pending.get("revision"):
                        continue
                    if parse_time(now) >= parse_time(latest_plan.get("next_review_at")):
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
        snapshots = {account: portfolio(self.store, account, quotes=quotes, now=now) for account in ("real", "paper")}
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
                if recommendation.get("action") in ("reduce", "exit") or recommendation.get("status") == "needs_review":
                    self._queue_review(stable_id("risk_change", plan["id"], plan.get("revision"), semantic), {
                        "symbol": plan["symbol"], "kind": "risk_change", "timestamp": now,
                        "plan_id": plan["id"], "revision": plan.get("revision"), "evidence": recommendation})
            except Exception as exc:
                self._error(result, "evaluate:" + plan["id"], exc)
            paper_key = "paper:" + plan["id"]
            paper_signature = stable_id(signature, _stable(snapshots["paper"]))
            try:
                advice = evaluate(self.store, plan, quote, snapshots["paper"], now=now)
                result["paper_recommendations"].append(advice)
                self.store.put("evaluation_cursor", paper_key, {"signature": paper_signature,
                    "recommendation_id": advice.get("id"), "timestamp": now})
            except Exception as exc:
                self._error(result, "paper_evaluate:" + plan["id"], exc)
        # Reservations can change during evaluation; publish the resulting cash
        # availability rather than the pre-evaluation planning snapshot.
        for account in snapshots:
            refreshed = portfolio(self.store, account, quotes=quotes, now=now)
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
                        self._queue_review(stable_id("quarterly_review", plan["id"], plan.get("revision"), key), {
                            "symbol": plan["symbol"], "kind": "quarterly_review", "timestamp": now,
                            "plan_id": plan["id"], "revision": plan.get("revision"), "evidence": {"period": key}})
        for key, trigger in self.store.items("review_trigger"):
            if trigger.get("processed") or not self.model or not self.execute or self._background_mode:
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
                self._export(result, "账户与资金", fields)
                for row in portfolio_rows(snapshot):
                    self._export(result, "持仓总览", row)
            for plan in plans:
                cursor = self.store.get("evaluation_cursor", plan["id"]) or {}
                recommendation = self.store.get("recommendation", cursor.get("recommendation_id", ""))
                self._export(result, "投资计划", plan_row(plan, recommendation))
            for key, report in self.store.items("report"):
                self._export(result, "复盘报告", report_row(report))
        for key, report in self.store.items("report"):
            if not self.store.get("notification", key):
                try:
                    receipt = self.feishu.notify(key, report["body"])
                    if self.execute and receipt.get("status") != "disabled":
                        self.store.put("notification", key, {"report_id": key, "receipt": receipt, "timestamp": now}, immutable=True)
                    elif not self.execute:
                        result["previews"].append(receipt)
                except Exception as exc:
                    self._error(result, "notification:" + key, exc)
        self.store.put("worker", "latest", {"timestamp": now, "execute": self.execute,
            "quote_count": len(result["quotes"]), "errors": result["errors"], "report_count": len(result["reports"]),
            "recommendation_count": len(result["recommendations"])})
        self._observe_health(result, now)
        if self._background_mode and self.execute and self.model:
            if not self._research_thread or not self._research_thread.is_alive():
                self._research_thread = threading.Thread(target=self._research_cycle,
                    args=(symbols, fundamentals), name="investment-research", daemon=True)
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
                   "ingestion": {"ingested": 0, "unchanged": 0, "plan_count": 0, "deferred_before_since_count": 0},
                   "reviewed_count": 0}
        try:
            if self.config.archive_database and not self._stop.is_set():
                ingestion = ingest_archives(self.store, self.config.archive_database, model=model,
                    symbols=symbols or None, fundamentals=fundamentals, now=self.clock(), research_since=self.config.research_since)
                summary["ingestion"] = {"ingested": ingestion.get("ingested", 0), "unchanged": ingestion.get("unchanged", 0),
                    "plan_count": len(ingestion.get("plans", [])),
                    "deferred_before_since_count": len(ingestion.get("deferred_before_since", []))}
                for error in ingestion.get("errors", []):
                    item = {"stage": "archive_ingestion", "error": _safe_error(error.get("error"))}
                    summary["deferred" if item["error"] == "ModelBudgetExceeded" else "errors"].append(item)
            for key, trigger in self.store.items("review_trigger"):
                if trigger.get("processed") or self._stop.is_set():
                    continue
                for plan in self._plans():
                    if plan["symbol"] == trigger["symbol"]:
                        review_plan(self.store, plan, model, event_id=key, evidence=trigger["evidence"], now=self.clock())
                        summary["reviewed_count"] += 1
                self.store.put("review_trigger", key, dict(trigger, processed=True, processed_at=self.clock()))
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
            deferred = research_state == "deferred" or any(code == "ModelBudgetExceeded" for _, code in issues)
            failures = sorted((stage, code) for stage, code in issues if code != "ModelBudgetExceeded")
            state = "blocked" if failures else "deferred" if deferred else "healthy"
            signature = stable_id(state, failures)
            previous = self.store.get("ops_health", "latest")
            if previous and previous.get("signature") == signature:
                return
            if not self.execute:
                return
            sequence = (previous or {}).get("sequence", 0) + 1
            transition = {"signature": signature, "status": state, "sequence": sequence,
                          "timestamp": now, "issues": [{"stage": a, "error": b} for a, b in failures]}
            # Save before attempting delivery: transport uncertainty must not cause
            # a new operational notice on every subsequent quote tick.
            self.store.put("ops_health", "latest", transition)
            if previous is None and state == "healthy":
                return
            key = stable_id("investment-operations", sequence, signature)
            title = "运行异常" if state == "blocked" else "研究延后" if state == "deferred" else "运行已恢复"
            lines = ["【犀牛投资·运维通知】" + title, "这是系统运行状态，非投资建议。", "时间：" + now]
            if failures:
                labels = {"research": "研究任务", "archive_ingestion": "封存证据研究", "official_quotes": "官方行情",
                          "feishu_input": "飞书账本读取", "notification": "报告通知", "export": "飞书投影"}
                lines.extend(labels.get(stage, "投资任务") + "：" + code for stage, code in failures)
            elif deferred:
                lines.append("研究预算等条件导致延后；未把延后计作研究成功或实际交易。")
            else:
                lines.append("此前异常已恢复；投资建议与实际成交仍以各自记录为准。")
            self.store.put("ops_notice", key, {"id": key, "status": "pending", "health": state, "timestamp": now})
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
            self._research_thread.join(timeout=30)


def status(store):
    outbox = [v for _, v in store.items("feishu_outbox")]
    return {"worker": store.get("worker", "latest"),
            "research_worker": store.get("research_worker", "latest"),
            "portfolio": portfolio(store, "real"), "paper_portfolio": portfolio(store, "paper"),
            "staged_count": len(store.items("ledger_staged")),
            "confirmed_count": len(store.events("ledger")), "plan_count": len(store.items("plan")),
            "pending_feishu": sum(v["status"] in ("pending", "unknown") for v in outbox),
            "model_calls": len(store.items("model_call")), "model_receipts": len(store.items("model_receipt"))}
