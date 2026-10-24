"""Independent notification review: no network or provider calls."""
import copy
import json
import threading
import unittest

from rhino_invest.config import Config
from rhino_invest.feishu import Feishu, UnknownOutcome
from rhino_invest.reporting import notification_text, plan_row, report_row
from rhino_invest.runner import Runner
from rhino_invest.store import Store


NOW = "2026-10-23T15:00:00Z"


class NotificationReviewTests(unittest.TestCase):
    def test_draft_free_text_cannot_bypass_hidden_price_columns(self):
        plan = {"id": "draft", "symbol": "TSM", "status": "needs_review", "revision": 1,
                "thesis": "买入 DRAFT_370_11，目标 DRAFT_800_22", "horizon_months": 24,
                "invalidation_conditions": ["DRAFT_270_33 止损"],
                "time_exit_policy": "涨到 DRAFT_900_44 离场",
                "entry": {"low": "370.11", "high": "375.22", "amount_usd": "2345.67"},
                "stop": {"price": "270.33"}, "targets": [{"price": "800.22"}]}
        text = json.dumps(plan_row(plan), ensure_ascii=False)
        for value in ("DRAFT_", "370.11", "375.22", "270.33", "800.22", "2345.67"):
            self.assertNotIn(value, text)

    def test_legacy_report_never_falls_back_to_private_audit_body(self):
        for kind in ("每日复盘", "每周复盘", "重大变化", "价格观察"):
            report = {"id": "legacy", "kind": kind, "timestamp": NOW, "period": "HASH_INTERNAL",
                      "account": "real", "portfolio": {"cash": "20000", "holdings_complete": False},
                      "body": "PRIVATE_DRAFT_370_800_270_None_False"}
            for text in (notification_text(report), report_row(report)["正文"]):
                self.assertNotIn("PRIVATE_DRAFT", text)
                self.assertNotIn("HASH_INTERNAL", text)
                self.assertIn("北京时间", text)
                self.assertLess(len(text), 700)

    def test_chat_readback_failure_preserves_provider_id_and_restart_only_reads(self):
        with Store(":memory:") as store:
            config = Config(feishu_chat_id="fixture-chat")
            client = Feishu(store, config, execute=True)
            calls = []
            body = None
            def first_request(method, path, **kwargs):
                nonlocal body
                calls.append((method, path))
                if method == "POST":
                    body = copy.deepcopy(kwargs["body"])
                    return {"message_id": "provider-message-1"}
                raise TimeoutError("offline readback unavailable")
            client._request = first_request
            with self.assertRaises(UnknownOutcome):
                client.notify("logical-notice", "简短中文摘要")
            operation = store.items("feishu_outbox")[0][1]
            self.assertEqual(operation["status"], "unknown")
            self.assertEqual(operation["result"]["message_id"], "provider-message-1")
            self.assertEqual(json.loads(body["content"])["text"], "简短中文摘要")
            self.assertNotIn("投资回执", body["content"])
            restarted = Feishu(store, config, execute=True)
            def recovered_request(method, path, **kwargs):
                calls.append((method, path))
                self.assertEqual(method, "GET")
                self.assertEqual(path, "/im/v1/messages/provider-message-1")
                return {"items": [{"message_id": "provider-message-1", "body": {"content": body["content"]}}]}
            restarted._request = recovered_request
            self.assertEqual(restarted.notify("logical-notice", "编辑正文不构成再次发送授权"),
                             {"message_id": "provider-message-1"})
            self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
            self.assertEqual(store.items("feishu_outbox")[0][1]["status"], "delivered")

    def test_unknown_without_provider_id_never_uses_same_text_or_new_route_as_receipt(self):
        with Store(":memory:") as store:
            client = Feishu(store, Config(feishu_chat_id="fixture-chat"), execute=True)
            sent = []
            def lost_request(method, path, **kwargs):
                sent.append((method, path))
                raise UnknownOutcome("offline response lost")
            client._request = lost_request
            with self.assertRaises(UnknownOutcome):
                client.notify("lost-notice", "同样的摘要")
            restarted = Feishu(store, Config(feishu_webhook_url=
                "https://open.feishu.cn/open-apis/bot/v2/hook/offline-fixture"), execute=True)
            def forbidden(*args, **kwargs):
                self.fail("No message ID exists: neither a text search nor a new send is authorized")
            restarted._request = forbidden
            with self.assertRaises(UnknownOutcome):
                restarted.notify("lost-notice", "同样的摘要")
            self.assertEqual(len(sent), 1)
            self.assertEqual(store.items("feishu_outbox")[0][1]["status"], "unknown")

    def test_wrong_message_id_or_edited_content_does_not_clear_unknown(self):
        for message_id, content in (("other-message", "original"), ("expected-message", "edited")):
            with self.subTest(message_id=message_id, content=content), Store(":memory:") as store:
                store.put("feishu_outbox", "unknown", {"id": "unknown", "identity": "notice:pending",
                    "status": "unknown", "result": {"message_id": "expected-message"},
                    "body": {"content": json.dumps({"text": "original"})}})
                client = Feishu(store, Config(feishu_chat_id="fixture-chat"), execute=True)
                client._request = lambda *args, **kwargs: {"items": [{"message_id": message_id,
                    "body": {"content": json.dumps({"text": content})}}]}
                with self.assertRaises(UnknownOutcome):
                    client.notify("pending", "original")
                self.assertEqual(store.get("feishu_outbox", "unknown")["status"], "unknown")

    def test_six_pending_stocks_and_repeated_ticks_only_send_scheduled_summaries(self):
        from tests_invest.test_acceptance_core import approved_plan
        from tests_invest.test_runner_delivery import Calendar, Feishu as OfflineFeishu, Market
        with Store(":memory:") as store:
            for symbol in ("GOOGL", "CRCL", "TSM", "NVDA", "META", "AAPL"):
                plan = approved_plan(symbol)
                plan["status"] = "needs_review"
                store.put("plan", plan["id"], plan)
            feishu, sent = OfflineFeishu(), []
            feishu.notify = lambda key, text: sent.append((key, text)) or {"status": "provider_ack"}
            runner = Runner(store, Config(symbols=()), market=Market(), feishu=feishu,
                            calendar=Calendar(), execute=True, clock=lambda: "2026-10-23T20:10:00Z")
            first = runner.run_once()
            self.assertEqual(first["errors"], [])
            self.assertEqual(len(sent), 4)  # Daily + weekly, explicitly separate real and paper.
            for _ in range(3):
                self.assertEqual(runner.run_once()["errors"], [])
            self.assertEqual(len(sent), 4)
            major = [report for _, report in store.items("report") if report.get("kind") == "重大变化"]
            self.assertEqual(len(major), 6)
            for report in major:
                self.assertEqual(store.get("notification", report["id"])["status"], "suppressed")
            for _, text in sent:
                self.assertNotIn("$150", text)
                self.assertIn("暂无新增操作建议", text)

    def test_old_scheduled_backlog_is_suppressed_separately_for_each_account(self):
        from tests_invest.test_runner_delivery import Calendar, Feishu as OfflineFeishu, Market
        with Store(":memory:") as store:
            stale_ids = []
            for account in ("real", "paper"):
                for kind in ("每日复盘", "每周复盘"):
                    for day in ("01", "02", "03"):
                        identity = account + kind + day
                        stale_ids.append(identity)
                        store.put("report", identity, {"id": identity, "kind": kind,
                            "account": account, "timestamp": f"2026-10-{day}T20:00:00Z",
                            "period": "historical-period-" + day, "portfolio": {"account": account},
                            "body": "PRIVATE_HISTORICAL_DRAFT"})
            feishu, sent = OfflineFeishu(), []
            feishu.notify = lambda key, text: sent.append((key, text)) or {"status": "provider_ack"}
            runner = Runner(store, Config(symbols=()), market=Market(), feishu=feishu,
                            calendar=Calendar(), execute=True, clock=lambda: "2026-10-23T20:10:00Z")
            self.assertEqual(runner.run_once()["errors"], [])
            self.assertEqual(len(sent), 4)
            self.assertFalse(set(stale_ids) & {key for key, _ in sent})
            for identity in stale_ids:
                self.assertEqual(store.get("notification", identity)["status"], "suppressed")
            self.assertEqual(runner.run_once()["errors"], [])
            self.assertEqual(len(sent), 4)

    def test_multiple_current_actions_stay_bounded_with_exit_rules_and_workbench(self):
        plans, recommendations = [], []
        for symbol in ("TSM", "NVDA", "GOOGL"):
            plans.append({"id": symbol, "symbol": symbol, "revision": 1, "status": "active",
                "next_review_at": "2027-01-01T00:00:00Z", "horizon_months": 24,
                "entry": {"low": "90", "high": "110"}, "targets": [{"price": "150"}],
                "take_profit": [{"price": "150", "fraction": "0.5"}],
                "stop": {"price": "80", "confirmation": "daily_close"}})
            recommendations.append({"plan_id": symbol, "symbol": symbol, "revision": 1,
                "account": "real", "status": "actionable", "action": "buy", "quantity": "10",
                "amount_usd": "1000", "holding_days": 120, "expires_at": "2026-10-23T20:00:00Z",
                "reasons": ["verified_entry_zone_and_reserved_cash"]})
        report = {"kind": "每日复盘", "timestamp": NOW, "account": "real",
                  "portfolio": {"cash": "20000", "positions": [], "holdings_complete": False}}
        text = notification_text(report, current_plans=plans, recommendations=recommendations,
                                 now=NOW, table_url="https://open.feishu.cn/base/Test123")
        self.assertLessEqual(len(text.splitlines()), 12)
        for value in ("分批止盈", "50%", "收盘确认", "24个月", "120天", "建议有效至", "另有2项", "https://open.feishu.cn/base/Test123"):
            self.assertIn(value, text)

    def test_real_thread_shutdown_does_not_return_before_request_receipt(self):
        with Store(":memory:") as store:
            release, started, returned, stop = (threading.Event() for _ in range(4))
            stop.set()
            runner = Runner(store, Config(), clock=lambda: NOW)
            def request():
                started.set()
                release.wait(5)
                store.put("offline_receipt", "current-request", {"received": True})
            research = threading.Thread(target=request)
            research.start()
            self.assertTrue(started.wait(1))
            runner._research_thread = research
            def shutdown():
                runner.serve(stop=stop)
                returned.set()
            serving = threading.Thread(target=shutdown)
            serving.start()
            try:
                self.assertFalse(returned.wait(0.05))
                self.assertIsNone(store.get("offline_receipt", "current-request"))
            finally:
                release.set()
                research.join(2)
                serving.join(2)
            self.assertFalse(research.is_alive())
            self.assertFalse(serving.is_alive())
            self.assertTrue(returned.is_set())
            self.assertTrue(store.get("offline_receipt", "current-request")["received"])
            self.assertEqual(store.get("worker_shutdown", "latest")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
