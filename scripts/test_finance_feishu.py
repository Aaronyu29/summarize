import hashlib
import hmac
import base64
import json
import pathlib
import tempfile
import unittest

from finance_feishu import Publisher, RemoteError, load_environment, prepare, signature, validate_doc_url


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, *, truncated=False, webhook_timeout=False):
        self.calls = []
        self.truncated = truncated
        self.webhook_timeout = webhook_timeout
        self.image_token = ""

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if "/hook/" in url:
            if self.webhook_timeout:
                raise TimeoutError("not sure whether server accepted message")
            return Response({"code": 0})
        if url.endswith("/tenant_access_token/internal"):
            return Response({"code": 0, "tenant_access_token": "PRIVATE_TOKEN_NOT_FOR_DISK"})
        if url.endswith("/medias/upload_all"):
            return Response({"code": 0, "data": {"file_token": "media-token"}})
        if method == "POST" and url.endswith("/import_tasks"):
            return Response({"code": 0, "data": {"ticket": "import-ticket"}})
        if "/import_tasks/" in url:
            return Response({"code": 0, "data": {"result": {
                "job_status": 0, "token": "document-token", "url": "https://example.feishu.cn/docx/document-token",
                "extra": {"truncated": True} if self.truncated else ["_pod_name"]}}})
        if "blocks?page_size" in url:
            return Response({"code": 0, "data": {"items": [{"block_id": "marker-id", "parent_id": "document-token",
                "block_type": 2, "text": {"elements": [{"text_run": {"content": "FINANCE_IMAGE_0000"}}]}}], "has_more": False}})
        if method == "GET" and url.endswith("/blocks/document-token"):
            return Response({"code": 0, "data": {"block": {"children": ["marker-id"]}}})
        if method == "POST" and url.endswith("/children"):
            return Response({"code": 0, "data": {"children": [{"block_id": "image-id"}]}})
        if method == "PATCH":
            if "replace_image" in kwargs["json"]:
                self.image_token = kwargs["json"]["replace_image"]["token"]
            return Response({"code": 0, "data": {}})
        if method == "GET" and url.endswith("/blocks/image-id"):
            return Response({"code": 0, "data": {"block": {"image": {"token": self.image_token}}}})
        if url.endswith("/raw_content"):
            return Response({"code": 0, "data": {"content": "Sample\nEvidence\nImage caption"}})
        raise AssertionError(f"Unexpected request {method}")


class FeishuTests(unittest.TestCase):
    def setUp(self):
        root = pathlib.Path(__file__).resolve().parent.parent / ".cache" / "finance-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.md = self.root / "brief.md"
        self.md.write_text("# Sample\n\nEvidence\n\n![Image caption](frame.jpg)\n", encoding="utf-8")
        (self.root / "frame.jpg").write_bytes(b"image bytes")
        self.env = {"FEISHU_APP_ID": "test-app", "FEISHU_APP_SECRET": "PRIVATE_SECRET_NOT_FOR_DISK",
                    "FEISHU_FOLDER_TOKEN": "folder-token", "LARK_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test"}

    def publisher(self, session):
        return Publisher(self.md, "Sample", "feishu", env=self.env, session=session, sleep=lambda _: None)

    def test_dry_run_prepares_local_images_without_network(self):
        content, images = prepare(self.md)
        self.assertIn("FINANCE_IMAGE_0000", content)
        self.assertEqual(len(images), 1)

    def test_existing_user_config_aliases_are_loaded_without_evaluation(self):
        path = self.root / "private.env"
        path.write_text('APPID=test-app\nAPPSecret="$(literal-secret)"\nLARK_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/test\n', encoding="utf-8")
        env = load_environment(path)
        self.assertEqual(env["FEISHU_APP_ID"], "test-app")
        self.assertEqual(env["FEISHU_APP_SECRET"], "$(literal-secret)")
        self.assertEqual(env["LARK_WEBHOOK_URL"], self.env["LARK_WEBHOOK_URL"])

    def test_folder_can_be_omitted_for_application_root(self):
        env = dict(self.env)
        env.pop("FEISHU_FOLDER_TOKEN")
        publisher = Publisher(self.md, "Sample", "feishu", env=env, session=FakeSession(), sleep=lambda _: None)
        self.assertEqual(publisher.folder, "")

    def test_parent_escape_and_remote_images_are_rejected(self):
        for image in ["../outside.jpg", "https://example.com/image.jpg"]:
            self.md.write_text(f"![bad]({image})", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare(self.md)

    def test_import_images_verify_then_send_and_deduplicate(self):
        session = FakeSession()
        publisher = self.publisher(session)
        result = publisher.run()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["image_count"], 1)
        self.assertTrue(session.calls[-1][1].endswith("/hook/test"))
        receipt = publisher.state_path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_SECRET", receipt)
        self.assertNotIn("PRIVATE_TOKEN", receipt)
        self.assertNotIn("/hook/", receipt)
        count = len(session.calls)
        self.assertEqual(self.publisher(session).run()["status"], "already_delivered")
        self.assertEqual(len(session.calls), count)

    def test_truncated_import_never_notifies(self):
        session = FakeSession(truncated=True)
        with self.assertRaisesRegex(RuntimeError, "truncation"):
            self.publisher(session).run()
        self.assertFalse(any("/hook/" in url for _, url in session.calls))

    def test_ambiguous_webhook_cannot_be_replayed_blindly(self):
        session = FakeSession(webhook_timeout=True)
        publisher = self.publisher(session)
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            publisher.run()
        before = len(session.calls)
        with self.assertRaisesRegex(RuntimeError, "Unreconciled"):
            self.publisher(session).run()
        self.assertEqual(len(session.calls), before)

    def test_definitive_permission_rejection_can_resume_after_permission_is_fixed(self):
        publisher = self.publisher(FakeSession())

        def rejected():
            raise RemoteError("API business code 99991672", definitive=True)

        with self.assertRaises(RemoteError):
            publisher.mutate("create_import", rejected, lambda _: None)
        self.assertNotIn("pending_write", publisher.state)
        self.assertEqual(publisher.state["last_rejection"]["step"], "create_import")
        self.assertEqual(self.publisher(FakeSession()).run()["status"], "delivered")

    def test_ambiguous_server_failure_remains_fenced(self):
        publisher = self.publisher(FakeSession())

        def server_failed():
            raise RemoteError("API HTTP status 500")

        with self.assertRaises(RemoteError):
            publisher.mutate("create_import", server_failed, lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "Unreconciled"):
            self.publisher(FakeSession()).run()

    def test_arbitrary_document_hosts_are_rejected(self):
        for url in ["https://feishu.cn.evil.example/docx/a", "http://x.feishu.cn/docx/a", "https://x.feishu.cn/wiki/a"]:
            with self.assertRaises(ValueError):
                validate_doc_url(url)

    def test_bot_signature_uses_timestamp_secret_as_key_and_empty_message(self):
        expected = base64.b64encode(hmac.new(b"123\nsecret", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(signature("123", "secret"), expected)


if __name__ == "__main__":
    unittest.main()
