import unittest
from decimal import Decimal
from unittest.mock import patch

from rhino_invest.store import Store
from rhino_invest.ledger import stage_entry, confirm_entry, correct_entry, import_csv, portfolio
from rhino_invest.paper import paper_account


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def entry(self, id, type, day=1, **fields):
        return {"id": id, "account": "real", "type": type, "timestamp": f"2026-01-{day:02}T12:00:00Z", "currency": "USD", **fields}

    def add(self, id, type, day=1, **fields):
        e = self.entry(id, type, day, **fields)
        stage_entry(self.store, e)
        return confirm_entry(self.store, id)

    def test_staging_requires_confirmation_and_real_target_is_not_cash(self):
        p = portfolio(self.store)
        self.assertEqual(p["target_total_usd"], "20000")
        self.assertIsNone(p["cash"])
        stage_entry(self.store, self.entry("c", "opening_cash", amount="5000", confirmed=True))
        self.assertIsNone(portfolio(self.store)["cash"])
        confirm_entry(self.store, "c")
        confirm_entry(self.store, "c")
        self.assertEqual(portfolio(self.store)["cash"], "5000")
        self.assertEqual(len(self.store.events("ledger")), 1)

    def test_partial_sale_average_cost_dividend_split_and_account_separation(self):
        self.add("c", "opening_cash", amount="10000")
        self.add("b1", "buy", 2, symbol="TSM", quantity="10", price="100", fee="1")
        self.add("b2", "buy", 3, symbol="TSM", quantity="10", price="120", fee="1")
        self.add("s", "sell", 4, symbol="TSM", quantity="5", price="150", fee="1")
        self.add("d", "dividend", 5, symbol="TSM", amount="10", fee="1")
        self.add("split", "split", 6, symbol="TSM", ratio="2")
        p = portfolio(self.store, quotes={"TSM": {"price": "80", "status": "historical", "quote_time": "2026-01-07T12:00:00Z", "source": "longbridge-openapi", "currency": "USD"}})
        position = p["positions"][0]
        self.assertEqual(position["quantity"], "30")
        self.assertEqual(Decimal(position["average_cost"]), Decimal("55"))
        self.assertEqual(Decimal(position["realized_pnl_gross"]), Decimal("200"))
        self.assertEqual(Decimal(position["realized_pnl_net"]), Decimal("198.5"))
        self.assertEqual(Decimal(p["cash"]), Decimal("8556"))
        self.assertEqual(p["dividend_income"], "10")
        self.assertIsNone(portfolio(self.store, "paper")["cash"])

    def test_unknown_cost_and_fees_never_create_net_profit(self):
        self.add("c", "opening_cash", amount="1000")
        self.add("o", "opening_position", symbol="TSM", quantity="10")
        self.add("s", "sell", 2, symbol="TSM", quantity="2", price="100", fee=None)
        p = portfolio(self.store, quotes={"TSM": "110"})
        self.assertIsNone(p["realized_pnl_gross"])
        self.assertIsNone(p["realized_pnl_net"])
        self.assertIsNone(p["positions"][0]["unrealized_pnl_net"])
        self.assertIsNone(p["cash"])
        self.assertFalse(p["cost_complete"])
        self.assertFalse(p["fees_complete"])

    def test_correction_preserves_history_replays_cash_and_requires_confirm(self):
        self.add("c", "opening_cash", amount="5000")
        self.add("b", "buy", 2, symbol="TSM", quantity="10", price="100", fee="1")
        replacement = self.entry("b", "buy", 2, symbol="TSM", quantity="5", price="100", fee="1")
        correction = correct_entry(self.store, "b", replacement, id="fix", timestamp="2026-01-03T12:00:00Z")
        self.assertEqual(portfolio(self.store)["cash"], "3999")
        confirm_entry(self.store, correction["id"])
        self.assertEqual(portfolio(self.store)["cash"], "4499")
        self.assertEqual(self.store.events("ledger")[1]["quantity"], "10")
        self.assertEqual(len(self.store.events("ledger")), 3)
        with self.assertRaises(ValueError):
            stage_entry(self.store, self.entry("b", "buy", 2, symbol="TSM", quantity="6", price="100", fee="1"))

    def test_oversell_cash_and_cross_account_correction_fail_atomically(self):
        self.add("c", "opening_cash", amount="1000")
        stage_entry(self.store, self.entry("b", "buy", symbol="TSM", quantity="20", price="100", fee="1"))
        with self.assertRaises(ValueError):
            confirm_entry(self.store, "b")
        self.assertEqual(len(self.store.events("ledger")), 1)
        correction = {**self.entry("fix", "correction"), "account": "paper", "corrects_id": "c", "replacement": None}
        stage_entry(self.store, correction)
        with self.assertRaises(ValueError):
            confirm_entry(self.store, "fix")
        self.assertEqual(portfolio(self.store)["cash"], "1000")

    def test_csv_partial_fills_and_blank_fee_staged_only(self):
        text = "成交编号,类型,成交时间,股票代码,数量,成交价格,费用\nf1,买入,2026-01-01T12:00:00Z,TSM,1,100,\nf2,买入,2026-01-01T12:00:00Z,TSM,2,100,1\n"
        result = import_csv(self.store, text)
        self.assertEqual(result["count"], 2)
        self.assertIsNone(result["staged"][0]["fee"])
        self.assertEqual(self.store.events("ledger"), [])
        result2 = import_csv(self.store, text)
        self.assertEqual(result2["errors"], [])
        self.assertEqual(len(self.store.items("ledger_staged")), 2)

    def test_unverified_or_stale_quotes_cannot_silently_value_nav(self):
        self.store.put("account_control", "real", {"confirmed": True, "holdings_complete": True})
        self.add("c", "opening_cash", amount="5000")
        self.add("b", "buy", 2, symbol="TSM", quantity="10", price="100", fee="0")
        self.assertIsNone(portfolio(self.store, quotes={"TSM": "120"})["total_equity"])
        quote = {"price": "120", "source": "longbridge-openapi", "currency": "USD", "status": "current", "quote_time": "2026-01-02T12:00:00Z", "permission_verified": True}
        stale = portfolio(self.store, quotes={"TSM": quote}, now="2026-01-02T13:00:00Z")
        self.assertIsNone(stale["total_equity"])
        self.assertIn("stale_quote", stale["positions"][0]["valuation_reasons"])
        fresh = portfolio(self.store, quotes={"TSM": quote}, now="2026-01-02T12:00:01Z")
        self.assertEqual(Decimal(fresh["total_equity"]), Decimal("5200"))
        self.assertEqual(Decimal(fresh["positions"][0]["portfolio_weight"]), Decimal("1200") / Decimal("5200"))
        self.assertEqual(fresh["valuation_status"], "current")

    def test_holding_start_survives_add_and_partial_sale_then_resets(self):
        self.add("c", "opening_cash", amount="5000")
        self.add("b1", "buy", 2, symbol="TSM", quantity="10", price="100", fee="0")
        self.add("b2", "buy", 3, symbol="TSM", quantity="10", price="100", fee="0")
        self.add("s1", "sell", 4, symbol="TSM", quantity="10", price="100", fee="0")
        first = portfolio(self.store, now="2026-01-05T12:00:00Z")["positions"][0]
        self.assertEqual(first["holding_days"], 3)
        self.assertEqual(first["first_fill_at"], "2026-01-02T12:00:00+00:00")
        self.add("s2", "sell", 5, symbol="TSM", quantity="10", price="100", fee="0")
        self.add("b3", "buy", 6, symbol="TSM", quantity="1", price="100", fee="0")
        later = portfolio(self.store, now="2026-01-07T12:00:00Z")["positions"][0]
        self.assertEqual(later["holding_days"], 1)
        self.assertEqual(later["first_fill_at"], "2026-01-06T12:00:00+00:00")

    def test_full_exit_keeps_unknown_historical_realized_pnl(self):
        self.add("o", "opening_position", symbol="TSM", quantity="2")
        self.add("s", "sell", 2, symbol="TSM", quantity="2", price="100", fee="0")
        state = portfolio(self.store)
        self.assertIsNone(state["realized_pnl_net"])
        self.assertIsNone(state["realized_pnl_gross"])
        self.assertEqual(state["positions"][0]["cost_basis"], "0")
        self.assertIsNone(state["positions"][0]["first_fill_at"])

    def test_late_manual_confirmation_is_a_labelled_restated_economic_snapshot(self):
        with patch("rhino_invest.ledger.utc_now", return_value="2026-01-04T12:00:00+00:00"):
            self.add("late-cash", "opening_cash", amount="5000")
            self.add("late-fill", "buy", 2, symbol="TSM", quantity="2", price="100", fee="0")
        state = portfolio(self.store, now="2026-01-02T13:00:00+00:00")
        self.assertEqual(state["positions"][0]["quantity"], "2")
        self.assertEqual(state["history_basis"], "restated_economic_time")
        self.assertTrue(state["contains_late_confirmations"])
        self.assertEqual(state["latest_recorded_at"], "2026-01-04T12:00:00+00:00")
        receipt = self.store.events("ledger_confirmation")[-1]
        self.assertEqual(receipt["recorded_at"], "2026-01-04T12:00:00+00:00")

    def test_unknown_dividend_fee_cannot_be_reported_as_net_income(self):
        self.add("cash", "opening_cash", amount="1000")
        self.add("div", "dividend", 2, symbol="TSM", amount="10", fee=None)
        state = portfolio(self.store)
        self.assertEqual(state["dividend_income"], "10")
        self.assertIsNone(state["dividend_income_net"])
        self.assertIsNone(state["total_realized_income_net"])
        self.assertEqual(state["realized_pnl_scope"], "trades_only_dividends_separate")

    def test_inventory_is_unknown_until_explicit_reconciliation_even_with_cash(self):
        self.add("cash", "opening_cash", amount="5000")
        state = portfolio(self.store)
        self.assertFalse(state["holdings_complete"])
        self.assertIsNone(state["total_equity"])
        self.assertFalse(state["valuation_complete"])
        self.assertEqual(state["known_holdings_market_value"], "0")
        self.store.put("account_control", "real", {"holdings_complete": True, "confirmed": False})
        self.assertFalse(portfolio(self.store)["holdings_complete"])
        self.store.put("account_control", "real", {"holdings_complete": True, "confirmed": True})
        reconciled = portfolio(self.store)
        self.assertTrue(reconciled["holdings_complete"])
        self.assertEqual(reconciled["total_equity"], "5000")
        self.assertEqual(reconciled["market_value_scope"], "complete_reconciled_inventory")
        self.assertFalse(portfolio(self.store, "paper")["holdings_complete"])
        paper_account(self.store)
        self.assertTrue(portfolio(self.store, "paper")["holdings_complete"])


if __name__ == "__main__":
    unittest.main()
