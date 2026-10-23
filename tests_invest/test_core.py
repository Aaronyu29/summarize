import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from rhino_invest.common import decimal_value, parse_time, stable_id, validate_plan
from rhino_invest.store import Store


def plan():
    return {"id": "p", "revision": 1, "symbol": "TSM", "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00", "next_review_at": "2026-04-01T00:00:00Z",
            "thesis": "Evidenced business case", "horizon_months": 24, "evidence_refs": ["evidence:1"],
            "entry": {"low": "100", "high": "110", "amount_usd": "2000"},
            "targets": [{"scenario": "base", "price": "150", "horizon_months": 24,
                         "method": "future EPS × PE", "assumptions": {"eps": "5", "pe": "30"}, "evidence_refs": ["evidence:1"]}],
            "take_profit": [{"price": "150", "fraction": "0.5"}, {"price": "160", "fraction": "0.5"}],
            "stop": {"price": "80", "confirmation": "daily_close"}, "invalidation_conditions": ["A sustained margin collapse"],
            "time_exit_policy": "review at 24 months", "valuation_basis": {"type": "scenario"},
            "currency": "USD", "price_unit": "per_adr", "is_adr": True,
            "verification": {"provenance": True, "currency": True, "unit": True, "adr": True}}


class CommonTests(unittest.TestCase):
    def test_decimal_and_time_reject_unknown_masquerading_as_zero(self):
        for invalid in (None, True, float("nan"), "Infinity", "garbage"):
            with self.assertRaises(ValueError):
                decimal_value(invalid)
        with self.assertRaises(ValueError):
            parse_time("2026-01-01T00:00:00")
        self.assertEqual(parse_time("2026-01-01T08:00:00+08:00").hour, 0)
        self.assertEqual(stable_id({"a": 1, "b": 2}), stable_id({"b": 2, "a": 1}))

    def test_plan_contract(self):
        valid = plan()
        self.assertEqual(validate_plan(valid), [])
        valid["horizon_months"] = 2
        valid["verification"]["adr"] = False
        valid["entry"]["amount_usd"] = "3500"
        valid["take_profit"][0]["fraction"] = "1"
        errors = validate_plan(valid)
        self.assertTrue(any("12–36" in x for x in errors))
        self.assertTrue(any("adr" in x for x in errors))
        self.assertTrue(any("1000–3000" in x for x in errors))
        self.assertTrue(any("exceed one" in x for x in errors))


class StoreTests(unittest.TestCase):
    def test_immutable_replay_durability_and_atomic_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "invest.sqlite"
            with Store(path) as store:
                self.assertTrue(store.put("x", "one", {"money": Decimal("1.20")}, immutable=True))
                self.assertFalse(store.put("x", "one", {"money": "1.20"}, immutable=True))
                with self.assertRaises(ValueError):
                    store.put("x", "one", {"money": "2"})
                self.assertTrue(store.append_event("audit", "e", {"message": "first"}))
                self.assertFalse(store.append_event("audit", "e", {"message": "first"}))
                with self.assertRaises(ValueError):
                    store.append_event("audit", "e", {"message": "changed"})
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.put("x", "rolledback", {"x": 1})
                        raise RuntimeError("fail")
                self.assertIsNone(store.get("x", "rolledback"))
                with store.transaction():
                    store.put("x", "outer", {"ok": 1})
                    try:
                        with store.transaction():
                            store.put("x", "inner", {"bad": 1})
                            raise ValueError("inner failure caught by caller")
                    except ValueError:
                        pass
                self.assertIsNone(store.get("x", "inner"))
                self.assertEqual(store.get("x", "outer"), {"ok": 1})
            with Store(path) as reopened:
                self.assertEqual(reopened.get("x", "one"), {"money": "1.20"})
                self.assertEqual(len(reopened.events("audit")), 1)


if __name__ == "__main__":
    unittest.main()
