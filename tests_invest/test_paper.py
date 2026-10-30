import unittest
from decimal import Decimal

from rhino_invest.store import Store
from rhino_invest.paper import paper_account, simulate
from rhino_invest.ledger import portfolio, stage_entry, confirm_entry


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.signal = {"id": "buy1", "account": "paper", "action": "buy", "status": "active", "symbol": "TSM", "timestamp": "2026-01-02T14:30:00Z", "expires_at": "2026-01-02T21:00:00Z", "amount_usd": "2000", "entry": {"low": "50", "high": "150"}}

    def tearDown(self):
        self.store.close()

    def quote(self, timestamp="2026-01-02T14:31:00Z", **fields):
        return {"symbol": "TSM", "timestamp": timestamp, "quote_time": timestamp, "depth_timestamp": timestamp,
                "session": "regular", "fresh": True, "source": "longbridge-openapi", "currency": "USD",
                "status": "current", "permission_verified": True, "price": "100", "bid": "99", "ask": "101",
                "depth_verified": True, "depth_source": "longbridge-openapi", "depth_size_unit": "shares",
                "bid_size": "1000", "ask_size": "1000", **fields}

    def test_bid_ask_next_quote_whole_shares_slippage_fee_and_gap(self):
        quotes = [self.quote("2026-01-02T14:29:59Z", ask="70", bid="69"), self.quote(session="premarket", ask="80", bid="79"), self.quote(ask="130", bid="129")]
        result = simulate(self.store, self.signal, quotes, now="2026-01-02T14:31:01Z")
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["quantity"], "15")
        self.assertEqual(Decimal(result["price"]), Decimal("130.0650"))
        self.assertEqual(result["fee"], "1")
        self.assertIsNone(portfolio(self.store, "real")["cash"])
        cash = portfolio(self.store, "paper")["cash"]
        again = simulate(self.store, self.signal, [], now="2026-01-03T00:00:00Z")
        self.assertEqual(again, result)
        self.assertEqual(portfolio(self.store, "paper")["cash"], cash)

    def test_asks_missing_stale_or_expired_skip_without_fill(self):
        quotes = [self.quote(ask=None), self.quote(fresh=False), self.quote("2026-01-02T22:00:00Z")]
        skipped = simulate(self.store, self.signal, quotes, now="2026-01-02T23:00:00Z")
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(portfolio(self.store, "paper")["cash"], "20000")
        self.assertEqual(len(self.store.items("paper_fill")), 0)

    def test_sell_gap_uses_next_bid_and_limits_position(self):
        simulate(self.store, self.signal, [self.quote()], now="2026-01-02T14:31:01Z")
        signal = {"id": "sell1", "account": "paper", "action": "sell", "status": "actionable", "symbol": "TSM", "timestamp": "2026-01-02T15:00:00Z", "expires_at": "2026-01-02T21:00:00Z", "quantity": "10"}
        result = simulate(self.store, signal, [self.quote("2026-01-02T15:01:00Z", bid="70", ask="71")], now="2026-01-02T15:01:01Z")
        self.assertEqual(Decimal(result["price"]), Decimal("69.9650"))
        self.assertEqual(result["action"], "sell")
        signal = {**signal, "id": "sell2", "quantity": "999"}
        self.assertEqual(simulate(self.store, signal, [self.quote("2026-01-02T15:02:00Z")], now="2026-01-02T15:02:01Z")["reason"], "insufficient_paper_position")

    def test_buy_gap_limit_and_forged_fresh_flag_do_not_fill(self):
        signal = {**self.signal, "entry": {"low": "90", "high": "105"}}
        result = simulate(self.store, signal, [self.quote(ask="130", bid="129")], now="2026-01-02T14:31:01Z")
        self.assertEqual(result["reason"], "post_signal_price_outside_entry_limits")
        self.assertEqual(len(self.store.items("paper_fill")), 0)
        stale = simulate(self.store, signal, [self.quote(fresh=True)], now="2026-01-02T15:00:00Z")
        self.assertEqual(stale["status"], "skipped")
        forged = simulate(self.store, signal, [self.quote(source="unverified", fresh=True)], now="2026-01-02T14:31:01Z")
        self.assertEqual(forged["status"], "skipped")

    def test_recommendation_identity_cannot_change_after_fill(self):
        simulate(self.store, self.signal, [self.quote()], now="2026-01-02T14:31:01Z")
        with self.assertRaises(ValueError):
            simulate(self.store, {**self.signal, "amount_usd": "3000"}, [self.quote()], now="2026-01-02T14:31:01Z")

    def test_real_recommendation_never_becomes_paper_trade(self):
        result = simulate(self.store, {**self.signal, "account": "real"}, [self.quote()], now="2026-01-02T14:31:01Z")
        self.assertEqual(result["reason"], "paper_account_recommendation_required")
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_permission_delay_depth_source_and_time_must_be_verified(self):
        for index, changed in enumerate(({"permission_verified": None}, {"delayed": True},
                {"depth_timestamp": None}, {"depth_verified": False},
                {"depth_timestamp": "2026-01-02T14:29:00Z"}, {"ask_size": "0"})):
            with self.subTest(changed=changed):
                result = simulate(self.store, {**self.signal, "id": "rejected-" + str(index)},
                                  [self.quote(**changed)], now="2026-01-02T14:31:01Z")
                self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_unknown_book_units_are_explicit_price_model_not_assumed_shares(self):
        result = simulate(self.store, self.signal, [self.quote(depth_size_unit="unknown", ask_size=None, bid_size=None)],
                          now="2026-01-02T14:31:01Z")
        self.assertEqual(result["status"], "filled")
        self.assertFalse(result["liquidity_verified"])
        self.assertTrue(result["assumed_no_market_impact"])
        self.assertEqual(result["displayed_size_unit"], "unknown")
        second = {**self.signal, "id": "known-shares"}
        too_small = simulate(self.store, second, [self.quote(ask_size="1")], now="2026-01-02T14:31:01Z")
        self.assertEqual(too_small["reason"], "insufficient_displayed_quote_depth")

    def test_requested_buy_is_not_silently_reduced_below_minimum_cash(self):
        paper_account(self.store)
        stage_entry(self.store, {"id": "paper-out", "account": "paper", "currency": "USD", "type": "withdrawal",
            "timestamp": "2026-01-02T14:00:00Z", "amount": "19500"})
        confirm_entry(self.store, "paper-out")
        result = simulate(self.store, self.signal, [self.quote()], now="2026-01-02T14:31:01Z")
        self.assertEqual(result["reason"], "insufficient_paper_cash_for_requested_buy")
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_secondary_timestamp_cannot_hide_future_official_quote_time(self):
        quote = self.quote(quote_time="2026-01-02T14:31:15Z")
        result = simulate(self.store, self.signal, [quote], now="2026-01-02T14:31:01Z")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.items("paper_fill"), [])


if __name__ == "__main__":
    unittest.main()
