"""Cross-module lifecycle acceptance; fixtures never contact paid/external APIs."""
import copy
import unittest
from decimal import Decimal

from rhino_invest.config import Config
from rhino_invest.decisions import evaluate
from rhino_invest.ledger import portfolio, stage_entry, confirm_entry
from rhino_invest.runner import Runner
from rhino_invest.research import seal_plan_review
from rhino_invest.feishu import Feishu, SCHEMA
from rhino_invest.reporting import plan_row
from rhino_invest.store import Store


DAY = "2026-10-23"


def approved_plan(symbol="SOXX"):
    return {"id": "independent:" + symbol, "revision": 1, "symbol": symbol,
            "status": "active", "created_at": "2026-10-22T15:00:00+00:00",
            "evidence_refs": ["sealed:1"], "thesis": "Verified multi-year fixture thesis",
            "horizon_months": 24, "next_review_at": "2027-01-22T15:00:00+00:00",
            "entry": {"low": "90", "high": "110", "amount_usd": "1000"},
            "targets": [{"scenario": "base", "price": "150", "horizon_months": 24,
                         "method": "eps_multiple", "assumptions": {"eps": "5", "multiple": "30"},
                         "evidence_refs": ["sealed:1"]}],
            "take_profit": [{"price": "150", "fraction": "0.5"}, {"price": "200", "fraction": "0.5"}],
            "stop": {"price": "80", "confirmation": "daily_close"},
            "invalidation_conditions": ["cashflow_failed"], "time_exit_policy": "re-review thesis at horizon",
            "valuation_basis": {"type": "scenario"}, "currency": "USD", "price_unit": "per_share",
            "verification": {"provenance": True, "currency": True, "unit": True}, "conditions_parsed": True}


def quote(now, price="100", symbol="SOXX"):
    return {"symbol": symbol, "next_regular_close": DAY + "T20:00:00+00:00", "selected": {
        "symbol": symbol, "price": price, "bid": str(Decimal(price) - 1), "ask": str(Decimal(price) + 1),
        "quote_time": now, "status": "current", "source": "longbridge-openapi", "currency": "USD",
        "session": "regular", "permission_verified": True, "depth_timestamp": now,
        "depth_source": "longbridge-openapi", "depth_verified": True, "depth_size_unit": "shares",
        "bid_size": "1000", "ask_size": "1000"}}


class FixtureMarket:
    price = "100"
    def snapshots(self, symbols, now=None):
        return [quote(now, self.price, symbol) for symbol in symbols]

    def fundamentals(self, symbol, now=None):
        return {"symbol": symbol, "source": "longbridge-openapi", "status": "fixture"}


class FixtureCalendar:
    def state(self, now):
        return {"source": "fixture:XNYS", "completed_session": "2026-10-22",
                "completed_close": "2026-10-22T20:00:00+00:00", "next_regular_close": DAY + "T20:00:00+00:00",
                "following_close": DAY + "T20:00:00+00:00", "weekly_due": False,
                "quarter_end_due": False, "week": "2026-W43", "quarter": "2026-Q4"}


class FixtureFeishu:
    app_token = None
    def upsert(self, table, fields):
        return {"status": "dry_run", "table": table}

    def notify(self, id, body):
        return {"status": "disabled"}


class AcceptanceCoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.put("research_evidence", "sealed:1", {"id": "sealed:1", "source_type": "sealed_analysis",
            "analysis_sha256": "a" * 64, "transcript_sha256": "b" * 64, "visual_sha256": "c" * 64}, immutable=True)
        self.plan = approved_plan()
        seal_plan_review(self.store, self.plan, {"accepted": True, "issues": []}, self.plan["evidence_refs"])
        self.store.put("plan", self.plan["id"], self.plan)
        # This acceptance fixture starts from an explicitly reconciled empty
        # inventory; individual tests can revoke that declaration.
        self.store.put("account_control", "real", {"confirmed": True, "holdings_complete": True})
        self.now = DAY + "T15:00:00+00:00"
        self.market = FixtureMarket()
        self.runner = Runner(self.store, Config(symbols=("SOXX",)), market=self.market,
                             feishu=FixtureFeishu(), calendar=FixtureCalendar(), clock=lambda: self.now)

    def confirm(self, id, kind, **fields):
        entry = {"id": id, "type": kind, "account": "real", "currency": "USD",
                 "timestamp": "2026-10-22T15:00:00+00:00", **fields}
        stage_entry(self.store, entry)
        confirm_entry(self.store, id)

    def tick(self, minute, price=None):
        self.now = f"{DAY}T15:{minute:02}:00+00:00"
        if price is not None:
            self.market.price = price
        return self.runner.run_once()

    def actionable(self, account, actions=("buy", "add", "reduce", "exit")):
        return [r for _, r in self.store.items("recommendation") if r["account"] == account
                and r["status"] == "actionable" and r["action"] in actions]

    def test_next_tick_paper_fills_once_real_waits_for_manual_confirmation(self):
        self.confirm("cash", "opening_cash", amount="5000")
        first = self.tick(0)
        self.assertEqual(first["errors"], [])
        self.assertEqual(self.store.items("paper_fill"), [])
        self.assertEqual(len(self.actionable("real")), 1)
        self.assertEqual(len(self.actionable("paper")), 1)
        signal = self.actionable("paper")[0]
        second = self.tick(1)
        self.assertEqual(second["errors"], [])
        fills = self.store.items("paper_fill")
        self.assertEqual(len(fills), 1)
        fill = fills[0][1]
        self.assertEqual(fill["recommendation_id"], signal["id"])
        self.assertGreater(fill["timestamp"], signal["timestamp"])
        self.assertEqual(Decimal(fill["price"]), Decimal("101.0505"))
        self.assertEqual(portfolio(self.store, "real", now=self.now)["cash"], "5000")
        paper_cash = portfolio(self.store, "paper", now=self.now)["cash"]
        self.tick(2)
        self.assertEqual(len(self.store.items("paper_fill")), 1)
        self.assertEqual(len(self.actionable("real")), 1)
        self.assertEqual(len(self.actionable("paper")), 1)
        # Later real entry does not rewrite the earlier shadow fill or account.
        self.confirm("user-fill", "buy", symbol="SOXX", quantity="3", price="102", fee="0",
                     timestamp=DAY + "T15:02:30+00:00")
        self.tick(3)
        self.assertEqual(portfolio(self.store, "real", now=self.now)["positions"][0]["quantity"], "3")
        self.assertEqual(portfolio(self.store, "paper", now=self.now)["cash"], paper_cash)
        self.assertEqual(self.store.get("paper_fill", signal["id"]), fill)

    def test_take_profit_half_stage_does_not_repeat_on_identical_price_ticks(self):
        self.tick(0)
        self.tick(1)
        self.tick(2, "151")
        exits = self.actionable("paper", ("reduce", "exit"))
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["quantity"], "4")
        self.tick(3)
        after_first_sale = portfolio(self.store, "paper", now=self.now)["positions"][0]["quantity"]
        self.assertEqual(after_first_sale, "5")
        self.tick(4)
        self.tick(5)
        self.assertEqual(len(self.actionable("paper", ("reduce", "exit"))), 1)
        self.assertEqual(portfolio(self.store, "paper", now=self.now)["positions"][0]["quantity"], "5")
        self.tick(6, "201")
        later = self.actionable("paper", ("reduce", "exit"))
        self.assertEqual(len(later), 2)
        self.assertIn("5", [r["quantity"] for r in later])
        self.tick(7)
        self.tick(8)
        self.assertEqual(portfolio(self.store, "paper", now=self.now)["positions"][0]["quantity"], "0")
        self.assertEqual(len(self.store.items("paper_fill")), 3)

    def test_unknown_real_funds_and_costs_never_borrow_paper_assets_or_fabricate_nav(self):
        self.confirm("unknown-baseline", "opening_position", symbol="SOXX", quantity="2")
        result = self.tick(0)
        self.assertEqual(result["errors"], [])
        self.assertIsNone(result["portfolio"]["cash"])
        self.assertIsNone(result["portfolio"]["total_equity"])
        self.assertIsNone(result["portfolio"]["positions"][0]["unrealized_pnl_net"])
        self.assertEqual(self.actionable("real", ("buy", "add")), [])
        self.assertEqual(len(self.actionable("paper", ("buy", "add"))), 1)
        staged = stage_entry(self.store, {"id": "unconfirmed", "account": "real", "currency": "USD",
            "type": "opening_cash", "timestamp": DAY + "T14:00:00Z", "amount": "5000"})
        self.assertEqual(staged["status"], "staged")
        self.assertIsNone(portfolio(self.store, "real", now=self.now)["cash"])
        with self.assertRaises(ValueError):
            stage_entry(self.store, {**staged, "amount": "6000"})

    def test_pending_intent_retains_original_cash_reservation_after_control_change(self):
        self.confirm("cash", "opening_cash", amount="2500")
        self.store.put("plan_control", self.plan["id"], {"confirmed": True, "amount_usd": "2000"})
        first = evaluate(self.store, self.plan, quote(self.now), portfolio(self.store, now=self.now), self.now)
        self.assertEqual(first["amount_usd"], "2000")
        self.store.put("plan_control", self.plan["id"], {"confirmed": True, "amount_usd": "1000"})
        again = evaluate(self.store, self.plan, quote(self.now), portfolio(self.store, now=self.now), self.now)
        self.assertEqual(again["id"], first["id"])
        other = approved_plan("NVDA")
        seal_plan_review(self.store, other, {"accepted": True, "issues": []}, other["evidence_refs"])
        blocked = evaluate(self.store, other, quote(self.now, symbol="NVDA"), portfolio(self.store, now=self.now), self.now)
        self.assertEqual(blocked["status"], "cash_blocked")

    def test_invalidated_plan_cancels_pending_paper_before_next_quote(self):
        self.tick(0)
        self.store.put("plan", self.plan["id"], {**self.plan, "status": "invalidated"})
        self.tick(1)
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_identical_price_crossing_review_time_is_re_evaluated(self):
        plan = {**self.plan, "next_review_at": DAY + "T15:01:00+00:00"}
        seal_plan_review(self.store, plan, {"accepted": True, "issues": []}, plan["evidence_refs"])
        self.store.put("plan", plan["id"], plan)
        self.confirm("cash", "opening_cash", amount="5000")
        self.tick(0)
        self.tick(1)
        cursor = self.store.get("evaluation_cursor", plan["id"])
        latest = self.store.get("recommendation", cursor["recommendation_id"])
        self.assertEqual(latest["status"], "needs_review")
        self.assertIn("scheduled_thesis_review_due", latest["reasons"])
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_reused_pre_signal_quote_never_becomes_a_later_paper_fill(self):
        self.tick(0)
        original = self.now
        self.market.snapshots = lambda symbols, now=None: [quote(original, symbol=s) for s in symbols]
        result = self.tick(1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.store.items("paper_fill"), [])
        self.assertEqual(result["paper"][0]["status"], "skipped")
        self.assertIn("quote_not_after_signal_or_outside_validity", result["paper"][0]["observations"])

    def test_bitable_opening_acquisition_split_correction_and_void_are_accessible(self):
        fields = {"系统标识": "ui-opening", "账户": "real", "类型": "期初持仓", "股票": "SOXX",
                  "数量": "10", "持仓成本总额美元": "1000", "费用美元": "0", "币种": "USD",
                  "首次建仓时间": "2025-10-20T15:00:00+00:00", "成交时间": "2026-10-22T15:00:00+00:00",
                  "确认状态": "确认"}
        rows = [{"record_id": "row-open", "fields": fields}, {"record_id": "row-split", "fields": {
            "系统标识": "ui-split", "账户": "real", "类型": "拆股", "股票": "SOXX", "拆股比例": "2",
            "成交时间": DAY + "T13:00:00+00:00", "确认状态": "确认"}}]
        client = Feishu(self.store, Config(), execute=True)
        client.records = lambda table: copy.deepcopy(rows) if table == "成交录入" else []
        client.upsert = lambda table, fields: {"status": "fixture", "fields": fields}
        imported = client.poll_inputs()
        self.assertEqual(imported["errors"], [])
        position = portfolio(self.store, now=self.now)["positions"][0]
        self.assertEqual(position["quantity"], "20")
        self.assertEqual(position["cost_basis"], "1000")
        self.assertEqual(position["first_fill_at"], "2025-10-20T15:00:00+00:00")
        correction = {**fields, "系统标识": "ui-correction", "类型": "更正", "原成交标识": "ui-opening",
                      "更正后类型": "期初持仓", "数量": "8", "持仓成本总额美元": "800",
                      "更正时间": DAY + "T14:00:00+00:00", "更正原因": "对账修正", "确认状态": "暂存"}
        rows[:] = [{"record_id": "row-correction", "fields": correction}]
        self.assertEqual(client.poll_inputs()["errors"], [])
        self.assertEqual(portfolio(self.store, now=self.now)["positions"][0]["quantity"], "20")
        correction["确认状态"] = "确认"
        self.assertEqual(client.poll_inputs()["errors"], [])
        self.assertEqual(portfolio(self.store, now=self.now)["positions"][0]["quantity"], "16")
        rows[:] = [{"record_id": "row-void", "fields": {"系统标识": "ui-void", "账户": "real", "类型": "作废",
            "原成交标识": "ui-split", "更正时间": DAY + "T14:01:00+00:00", "更正原因": "拆股重复录入", "确认状态": "确认"}}]
        self.assertEqual(client.poll_inputs()["errors"], [])
        self.assertEqual(portfolio(self.store, now=self.now)["positions"][0]["quantity"], "8")
        originals = {e["id"]: e for e in self.store.events("ledger")}
        self.assertEqual(originals["ui-opening"]["quantity"], "10")
        self.assertEqual(originals["ui-split"]["ratio"], "2")
        self.assertTrue({"拆股比例", "持仓成本总额美元", "首次建仓时间", "原成交标识", "更正时间"}.issubset(SCHEMA["成交录入"]))

    def test_long_horizon_plan_ui_renders_duration_profit_steps_and_risk_readably(self):
        self.tick(0)
        advice = self.actionable("paper")[0]
        row = plan_row(self.plan, advice)
        self.assertEqual(row["期限月数"], "24")
        self.assertIn("50", row["分批止盈"])
        self.assertNotIn('"fraction"', row["分批止盈"])
        self.assertNotEqual(row["止损确认方式"], "daily_close")
        self.assertIn("未配置", row["风险状态"])
        self.assertIn("美元", "".join(row.keys()))
        self.assertTrue(row["建议有效期"])

    def test_drawdown_control_needs_explicit_confirmation_and_unknown_peak_never_passes(self):
        self.confirm("cash", "opening_cash", amount="5000")
        original_control = self.store.get("account_control", "real")
        setting = {"账户": "real", "回撤硬限制设置": "0.10", "设置确认状态": "待暂存"}
        rows = [{"record_id": "risk-setting", "fields": setting}]
        client = Feishu(self.store, Config(), execute=True)
        client.records = lambda table: copy.deepcopy(rows) if table == "账户与资金" else []
        client.upsert = lambda table, fields: {"status": "fixture"}
        self.assertEqual(client.poll_inputs()["errors"], [])
        self.assertEqual(self.store.get("account_control", "real"), original_control)
        self.assertFalse(self.store.get("account_control_staged", "real")["confirmed"])
        setting["设置确认状态"] = "确认设置"
        self.assertEqual(client.poll_inputs()["errors"], [])
        result = self.tick(0)
        advice = result["recommendations"][0]
        self.assertEqual(advice["risk_context"]["drawdown_status"], "not_evaluable")
        self.assertFalse(advice["risk_context"]["risk_check_passed"])
        self.assertNotIn(advice["action"], ("buy", "add"))
        self.assertEqual(result["portfolio"]["drawdown_limit"], "0.10")
        self.assertTrue(any("0.10" in r["body"] and "不能评估" in r["body"] for r in result["reports"]))

    def test_confirmed_real_cash_does_not_imply_unknown_inventory_is_empty(self):
        self.store.put("account_control", "real", {"confirmed": True, "holdings_complete": False})
        self.confirm("cash", "opening_cash", amount="5000")
        result = self.tick(0)
        real = result["portfolio"]
        self.assertEqual(real["cash"], "5000")
        self.assertFalse(real["holdings_complete"])
        self.assertIsNone(real["total_equity"])
        self.assertEqual(real["market_value_scope"], "recorded_positions_only_inventory_incomplete")
        self.assertEqual(self.actionable("real", ("buy", "add")), [])
        self.assertIn("actual_holdings_not_reconciled", result["recommendations"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
