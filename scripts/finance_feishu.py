"""Markdown -> Feishu/Lark docx (with local images) -> webhook link.

Defaults to a local dry run. Live calls require --execute and environment config.
Receipts retain import tickets and document/image IDs, never credentials.
Ambiguous writes stop for reconciliation instead of creating duplicate documents.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import pathlib
import re
import time
import urllib.parse
from collections import Counter

BASES = {"feishu": "https://open.feishu.cn/open-apis", "lark": "https://open.larksuite.com/open-apis"}
IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
ENV_KEYS = {"FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_FOLDER_TOKEN", "LARK_WEBHOOK_URL", "LARK_WEBHOOK_SECRET"}
ENV_ALIASES = {"APPID": "FEISHU_APP_ID", "APPSecret": "FEISHU_APP_SECRET", "LARK_WEBHOOK": "LARK_WEBHOOK_URL"}


def highlighted_elements(text):
    """Convert editorial emphasis to native Feishu bold with a pale yellow background."""
    elements = []
    for index, fragment in enumerate(re.split(r'\*\*([^*\n]+)\*\*', text)):
        if fragment:
            run = {'content': fragment}
            if index % 2:
                run['text_element_style'] = {'bold': True, 'background_color': 3}
            elements.append({'text_run': run})
    return elements


def text_payload(block):
    return next((value for value in block.values()
                 if isinstance(value, dict) and isinstance(value.get('elements'), list)), {})


def bold_content(block, *, highlighted=False):
    """Keep adjacent styled runs together, but never bridge an unstyled gap."""
    return ''.join(run.get('content', '') if run.get('text_element_style', {}).get('bold')
                   and (not highlighted or run.get('text_element_style', {}).get('background_color') == 3) else '\0'
                   for element in text_payload(block).get('elements', [])
                   for run in [element.get('text_run', {})])


def load_environment(path):
    env = dict(os.environ)
    if path is not None:
        for number, line in enumerate(pathlib.Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            key = ENV_ALIASES.get(key, key)
            if not separator or key not in ENV_KEYS:
                raise ValueError(f"Invalid configuration key on line {number}")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key] = value
    return env


def prepare(markdown_path):
    markdown_path = pathlib.Path(markdown_path).resolve()
    original = markdown_path.read_text(encoding="utf-8")
    if "FINANCE_IMAGE_" in original:
        raise ValueError("Input contains reserved image markers")
    images = []

    def replace(match):
        relative = pathlib.Path(match[2])
        path = (markdown_path.parent / relative).resolve()
        if (urllib.parse.urlsplit(match[2]).scheme or relative.is_absolute()
                or not path.is_relative_to(markdown_path.parent)):
            raise ValueError("Only local images inside the Markdown directory are supported")
        if not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError("Image is missing, empty, or exceeds the upload limit")
        marker = f"FINANCE_IMAGE_{len(images):04}"
        images.append({"marker": marker, "path": str(path), "caption": match[1],
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return f"\n\n{marker}\n\n"

    content = IMAGE.sub(replace, original)
    if not content.strip() or len(content.encode("utf-8")) > 20 * 1024 * 1024:
        raise ValueError("Markdown is empty or exceeds the import limit")
    return content, images


def signature(timestamp, secret):
    return base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()


def validate_doc_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
            or not any(host == domain or host.endswith("." + domain) for domain in ["feishu.cn", "larksuite.com"])
            or not parsed.path.startswith("/docx/")):
        raise ValueError("Import did not return a valid Feishu/Lark docx URL")
    return url


class RemoteError(RuntimeError):
    """A definitive API rejection, without response bodies or secret-bearing URLs."""

    def __init__(self, message, *, definitive=False):
        super().__init__(message)
        self.definitive = definitive


class Publisher:
    def __init__(self, markdown_path, title, platform, env=None, session=None, sleep=time.sleep):
        self.env = os.environ if env is None else env
        self.markdown_path = pathlib.Path(markdown_path).resolve()
        self.content, self.images = prepare(self.markdown_path)
        self.title, self.base, self.sleep = title, BASES[platform], sleep
        self.folder = self.env.get("FEISHU_FOLDER_TOKEN", "")
        self.webhook = self.env.get("LARK_WEBHOOK_URL", "")
        parsed = urllib.parse.urlsplit(self.webhook)
        if (parsed.scheme != "https" or parsed.hostname not in {"open.feishu.cn", "open.larksuite.com"}
                or parsed.username or parsed.password or parsed.port
                or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", parsed.path)
                or parsed.query or parsed.fragment):
            raise ValueError("Configure a valid LARK_WEBHOOK_URL")
        if self.folder and not re.fullmatch(r"[A-Za-z0-9_-]+", self.folder):
            raise ValueError("FEISHU_FOLDER_TOKEN must be a Drive folder token")
        for name in ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
            if not self.env.get(name):
                raise ValueError(f"Missing {name}")
        if session is None:
            import requests
            session = requests.Session()
        self.session, self.token = session, None
        identity = json.dumps({"content": self.content, "title": title, "folder": self.folder,
                               "base": self.base, "webhook": hashlib.sha256(self.webhook.encode()).hexdigest(),
                               "images": [image["sha256"] for image in self.images]}, sort_keys=True)
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.state_path = self.markdown_path.parent / f".feishu-state-{self.identity[:16]}.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {
            "schema_version": 1, "identity": self.identity, "images": {}}
        if self.state["identity"] != self.identity:
            raise ValueError("Delivery receipt identity mismatch")

    def save(self):
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def call(self, method, path, *, json_body=None, data=None, files=None, auth=True):
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        self.sleep(0.4)
        try:
            response = self.session.request(method, self.base + path, headers=headers,
                                            json=json_body, data=data, files=files,
                                            timeout=(10, 30), allow_redirects=False)
        except Exception:
            raise RuntimeError("API transport failed; inspect saved receipt before retrying a write") from None
        if response.status_code != 200:
            try:
                detail = response.json()
                message = str(detail.get("msg", ""))
                for value in [self.token, *[self.env.get(name) for name in ENV_KEYS]]:
                    if value:
                        message = message.replace(value, "[redacted]")
                reason = f"; business_code={detail.get('code')}; {message[:1800]}"
            except (ValueError, AttributeError):
                reason = "; non-JSON error response"
            raise RemoteError(f"API HTTP status {response.status_code}{reason}",
                              definitive=response.status_code in {400, 401, 403, 404, 413, 422, 429})
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("API returned invalid JSON") from None
        if body.get("code") != 0:
            raise RemoteError(f"API business code {body.get('code', 'missing')}",
                              definitive=isinstance(body.get("code"), int))
        return body

    def mutate(self, label, function, save_result):
        self.state["pending_write"] = label
        self.save()
        try:
            result = function()
        except RemoteError as error:
            if error.definitive:
                self.state["last_rejection"] = {"step": label, "reason": str(error)}
                self.state.pop("pending_write", None)
                self.save()
            raise
        save_result(result)
        self.state.pop("pending_write", None)
        self.save()
        return result

    def upload(self, path, parent_type, parent_node=None):
        path = pathlib.Path(path)
        data = {"file_name": path.name, "parent_type": parent_type, "size": str(path.stat().st_size)}
        if parent_node:
            data["parent_node"] = parent_node
        if parent_type == "ccm_import_open":
            data["extra"] = json.dumps({"obj_type": "docx", "file_extension": "md"})
        with path.open("rb") as stream:
            return self.call("POST", "/drive/v1/medias/upload_all", data=data,
                             files={"file": (path.name, stream)})["data"]["file_token"]

    def list_blocks(self, doc):
        blocks, cursor = [], ""
        for _ in range(100):
            path = f"/docx/v1/documents/{doc}/blocks?page_size=500"
            if cursor:
                path += "&page_token=" + urllib.parse.quote(cursor, safe="")
            data = self.call("GET", path)["data"]
            blocks.extend(data["items"])
            if not data.get("has_more"):
                return blocks
            cursor = data.get("page_token")
            if not cursor:
                raise RuntimeError("Block pagination missing cursor")
        raise RuntimeError("Document block pagination exceeded limit")

    def block(self, doc, block):
        return self.call("GET", f"/docx/v1/documents/{doc}/blocks/{block}")["data"]["block"]

    def emphasize_and_verify(self, doc):
        normalize = lambda value: re.sub(r'\s+', '', value)
        expected = Counter(normalize(value) for value in re.findall(r'\*\*([^*\n]+)\*\*', self.content))
        if not expected:
            return
        blocks = self.list_blocks(doc)
        actual = [normalize(bold_content(block)) for block in blocks]
        if any(sum(value.count(phrase) for value in actual) < count for phrase, count in expected.items()):
            raise RuntimeError('Document lost required emphasis during import; webhook not sent')
        for block in blocks:
            # Headings already have a visual hierarchy. Highlight inline emphasis in body/list/quote blocks.
            if block.get('block_type') not in {2, 12, 13, 15}:
                continue
            elements = copy.deepcopy(text_payload(block).get('elements', []))
            changed = False
            for element in elements:
                run = element.get('text_run', {})
                style = run.get('text_element_style', {})
                if style.get('bold') and style.get('background_color') != 3:
                    style['background_color'] = 3
                    changed = True
            if changed:
                self.call('PATCH', f"/docx/v1/documents/{doc}/blocks/{block['block_id']}",
                          json_body={'update_text_elements': {'elements': elements}})
                confirmed = self.block(doc, block['block_id'])
                # Feishu may split a run when applying styles. Compare content and coverage, not segmentation.
                expected_content = normalize(bold_content({'text': {'elements': elements}}))
                confirmed_content = normalize(bold_content(confirmed, highlighted=True))
                if expected_content != confirmed_content:
                    raise RuntimeError('Document highlight readback failed; webhook not sent')
        final = [normalize(bold_content(block, highlighted=True)) for block in self.list_blocks(doc)]
        if any(sum(value.count(phrase) for value in final) < count for phrase, count in expected.items()):
            raise RuntimeError('Document highlight readback failed; webhook not sent')
        self.state['highlight_verified'] = True
        self.state['highlight_span_count'] = sum(expected.values())
        self.save()

    def run(self, *, notify=True):
        lock = self.state_path.with_suffix(".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.close(descriptor)
            self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else self.state
            if self.state.get("pending_write"):
                raise RuntimeError(f"Unreconciled write: {self.state['pending_write']}; do not blindly resend")
            if self.state.get("webhook_sent"):
                return {"status": "already_delivered", "url": self.state["url"]}
            auth = self.call("POST", "/auth/v3/tenant_access_token/internal", auth=False,
                             json_body={"app_id": self.env["FEISHU_APP_ID"], "app_secret": self.env["FEISHU_APP_SECRET"]})
            self.token = auth["tenant_access_token"]
            staged = self.markdown_path.parent / f".feishu-import-{self.identity[:16]}.md"
            if not self.state.get("ticket") and not self.state.get("doc_token"):
                staged.write_text(self.content, encoding="utf-8")
                file_token = self.upload(staged, "ccm_import_open")
                self.mutate("create_import", lambda: self.call("POST", "/drive/v1/import_tasks", json_body={
                    "file_extension": "md", "file_token": file_token, "type": "docx", "file_name": self.title,
                    "point": {"mount_type": 1, "mount_key": self.folder}}),
                    lambda response: self.state.update(ticket=response["data"]["ticket"]))
            if not self.state.get("doc_token"):
                for _ in range(30):
                    result = self.call("GET", f"/drive/v1/import_tasks/{self.state['ticket']}")["data"]["result"]
                    status = result.get("job_status")
                    if status == 0 and result.get("token"):
                        # Feishu may include internal metadata keys on successful imports.
                        # Keep rejecting unknown extras so conversion warnings are reviewed.
                        if any(item != "_pod_name" for item in (result.get("extra") or [])):
                            raise RuntimeError("Import returned warnings/extra information; check truncation before publishing")
                        url = validate_doc_url(result.get("url", ""))
                        self.state.update(doc_token=result["token"], url=url)
                        self.save()
                        break
                    if status not in {0, 1, 2}:
                        raise RemoteError(f"Import failed with job_status={status}")
                    self.sleep(1)
                else:
                    raise RuntimeError("Import still pending; saved ticket will be polled on the next run")
            doc = self.state["doc_token"]
            blocks = self.list_blocks(doc)
            for image in self.images:
                if hashlib.sha256(pathlib.Path(image["path"]).read_bytes()).hexdigest() != image["sha256"]:
                    raise RuntimeError("Local image changed after delivery identity was captured")
                marker = image["marker"]
                receipt = self.state["images"].setdefault(marker, {})
                if not receipt.get("marker_block"):
                    matches = [block for block in blocks if block.get("block_type") == 2 and
                               "".join(element.get("text_run", {}).get("content", "")
                                       for element in block.get("text", {}).get("elements", [])) == marker]
                    if len(matches) != 1:
                        raise RuntimeError("Image marker was lost or duplicated during Markdown import")
                    receipt.update(marker_block=matches[0]["block_id"], parent=matches[0]["parent_id"])
                    self.save()
                if not receipt.get("block_id"):
                    parent = self.block(doc, receipt["parent"])
                    index = parent["children"].index(receipt["marker_block"]) + 1
                    self.mutate(f"create_image:{marker}", lambda: self.call("POST",
                        f"/docx/v1/documents/{doc}/blocks/{receipt['parent']}/children",
                        json_body={"index": index, "children": [{"block_type": 27, "image": {}}]}),
                        lambda response: receipt.update(block_id=response["data"]["children"][0]["block_id"]))
                if not receipt.get("file_token"):
                    receipt["file_token"] = self.upload(image["path"], "docx_image", receipt["block_id"])
                    self.save()
                self.call("PATCH", f"/docx/v1/documents/{doc}/blocks/{receipt['block_id']}",
                          json_body={"replace_image": {"token": receipt["file_token"]}})
                confirmed = self.block(doc, receipt["block_id"])
                if confirmed.get("image", {}).get("token") != receipt["file_token"]:
                    raise RuntimeError("Uploaded image did not bind to the document")
                self.call("PATCH", f"/docx/v1/documents/{doc}/blocks/{receipt['marker_block']}", json_body={
                    "update_text_elements": {"elements": [{"text_run": {"content": image['caption']}}]}})
                receipt["verified"] = True
                self.save()
            raw = self.call("GET", f"/docx/v1/documents/{doc}/raw_content")["data"]["content"]
            headings = re.findall(r"^#{1,6}\s+(.+)$", self.content, flags=re.MULTILINE)
            normalized_raw = re.sub(r"\s", "", raw)
            if ("FINANCE_IMAGE_" in raw or not raw.strip() or
                    any(re.sub(r"\s", "", heading) not in normalized_raw for heading in headings)):
                raise RuntimeError("Document content verification failed; webhook not sent")
            self.emphasize_and_verify(doc)
            self.state["document_verified"] = True
            self.save()
            if not notify:
                return {"status": "document_verified", "url": self.state["url"],
                        "image_count": len(self.images), "highlight_span_count": self.state.get('highlight_span_count', 0)}
            payload = {"msg_type": "text", "content": {"text": f"{self.title}\n{self.state['url']}"}}
            if self.env.get("LARK_WEBHOOK_SECRET"):
                payload["timestamp"] = str(int(time.time()))
                payload["sign"] = signature(payload["timestamp"], self.env["LARK_WEBHOOK_SECRET"])

            def notify():
                try:
                    response = self.session.request("POST", self.webhook, json=payload, timeout=(10, 30), allow_redirects=False)
                except Exception:
                    raise RuntimeError("Webhook outcome unknown; receipt retained, no automatic resend") from None
                if response.status_code != 200:
                    raise RemoteError(f"Webhook HTTP status {response.status_code}",
                                      definitive=response.status_code in {400, 401, 403, 404, 413, 422, 429})
                result = response.json()
                code = result.get("code", result.get("StatusCode"))
                if code != 0:
                    raise RemoteError(f"Webhook business code {code}", definitive=isinstance(code, int))
                return result

            self.mutate("send_webhook", notify, lambda _: self.state.update(webhook_sent=True))
            return {"status": "delivered", "url": self.state["url"], "image_count": len(self.images)}
        finally:
            lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown", type=pathlib.Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--platform", choices=BASES, default="feishu")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env-file", type=pathlib.Path)
    args = parser.parse_args()
    if args.execute:
        print(json.dumps(Publisher(args.markdown, args.title, args.platform, env=load_environment(args.env_file)).run(), ensure_ascii=False))
    else:
        content, images = prepare(args.markdown)
        print(json.dumps({"status": "dry_run_no_network", "markdown_bytes": len(content.encode("utf-8")),
                          "image_count": len(images), "title": args.title,
                          "steps": ["upload_markdown", "import_docx", "upload_and_verify_local_images", "send_document_link"]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
