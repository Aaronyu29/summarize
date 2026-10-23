"""Bitable provisioning, staged manual input and durable idempotent delivery.

All business writes require execute=True. Bitable and app messages use readback;
custom bot webhooks record provider acceptance only. Uncertain requests are never
blindly resent. The local SQLite outbox survives restarts.
"""
from __future__ import annotations

import json
import time
import base64
import hashlib
import hmac
from urllib.parse import quote
import requests

from .common import stable_id, utc_now, decimal_value
from .ledger import stage_entry, confirm_entry


SCHEMA = {
    "账户与资金": ["系统标识", "账户", "币种", "目标总资产美元", "实际现金美元", "持仓市值美元", "总权益美元", "现金完整", "成本完整", "费用完整", "回撤限制", "回撤硬限制设置", "设置确认状态", "录入标识", "输入类型", "录入金额美元", "录入时间", "确认状态", "处理状态", "更新时间"],
    "成交录入": ["系统标识", "账户", "类型", "股票", "数量", "成交价美元", "费用美元", "金额美元", "币种", "成交时间", "推荐标识", "入场批次", "确认状态", "处理状态", "错误说明"],
    "持仓总览": ["系统标识", "账户", "股票", "数量", "成本", "市价", "市值", "未实现盈亏", "持有天数", "占总资产比例", "数据状态", "更新时间"],
    "投资计划": ["系统标识", "股票", "版本", "状态", "建议动作", "推荐标识", "入场批次", "理由", "投资逻辑", "期限月数", "买入区间", "单次买入美元", "单次投入设置美元", "入场批次设置", "设置确认状态", "目标情景", "分批止盈", "止损价", "止损确认方式", "逻辑失效条件", "到期处理", "下次复核", "证据引用", "风险状态", "更新时间"],
    "复盘报告": ["系统标识", "报告类型", "报告周期", "账户", "现金美元", "持仓市值美元", "总权益美元", "回撤限制", "正文", "生成时间"],
}
SCHEMA["投资计划"].extend(["持有天数", "建议有效期", "每股止损风险美元", "价格止损风险百分比", "本次投入止损风险美元", "当前持仓止损风险美元", "情景年化收益估算"])
SCHEMA["账户与资金"].extend(["规划预留美元", "可用现金美元"])
SCHEMA["成交录入"].extend(["拆股比例", "持仓成本总额美元", "成本总额美元", "首次建仓时间", "原成交标识", "更正后类型", "更正时间", "更正原因"])
SCHEMA["账户与资金"].append("错误说明")
SCHEMA["账户与资金"].extend(["持仓核对设置", "持仓核对状态"])
SCHEMA["投资计划"].append("设置错误说明")
for _settings_table in ("账户与资金", "投资计划"):
    SCHEMA[_settings_table].append("设置回执")

TYPE_NAMES = {"期初现金": "opening_cash", "期初持仓": "opening_position", "买入": "buy", "卖出": "sell", "入金": "deposit", "出金": "withdrawal", "股息": "dividend", "拆股": "split", "更正": "correction", "作废": "correction"}


class FeishuError(RuntimeError):
    pass


class UnknownOutcome(FeishuError):
    pass


class Feishu:
    def __init__(self, store, config, *, http=None, execute=False):
        self.store, self.config = store, config
        self.http = http or requests.Session()
        self.execute = bool(execute)
        self.token, self.token_until = None, 0
        saved = store.get("feishu_meta", "app") or {}
        self.app_token = config.feishu_app_token or saved.get("app_token")

    def _auth(self):
        if self.token and time.time() < self.token_until:
            return self.token
        if not self.config.feishu_app_id or not self.config.feishu_app_secret:
            raise FeishuError("Feishu app credentials are not configured")
        try:
            response = self.http.request("POST", self.config.feishu_base_url + "/auth/v3/tenant_access_token/internal",
                                         json={"app_id": self.config.feishu_app_id, "app_secret": self.config.feishu_app_secret}, timeout=30)
            body = response.json()
            if response.status_code >= 400 or body.get("code", 0) or not body.get("tenant_access_token"):
                raise FeishuError("Feishu authentication failed; check app permissions and credentials")
            self.token = body["tenant_access_token"]
            self.token_until = time.time() + int(body.get("expire", 3600)) - 60
            return self.token
        except FeishuError:
            raise
        except Exception:
            raise FeishuError("Feishu authentication transport failed") from None

    def _request(self, method, path, *, body=None, params=None):
        try:
            response = self.http.request(method, self.config.feishu_base_url + path,
                                         headers={"Authorization": "Bearer " + self._auth()},
                                         json=body, params=params, timeout=30)
        except FeishuError:
            raise
        except Exception:
            raise UnknownOutcome("Feishu transport outcome is unknown; reconcile before retry") from None
        try:
            data = response.json()
        except Exception:
            raise UnknownOutcome("Feishu returned an unreadable response; reconcile before retry") from None
        if response.status_code >= 500:
            raise UnknownOutcome("Feishu server outcome is unknown; reconcile before retry")
        if response.status_code >= 400 or data.get("code", 0):
            raise FeishuError(f"Feishu request rejected (HTTP {response.status_code}, code {data.get('code', 'unknown')})")
        return data.get("data", {})

    def _list(self, path, *, params=None):
        result, cursor = [], None
        while True:
            args = dict(params or {}, page_size=100)
            if cursor:
                args["page_token"] = cursor
            data = self._request("GET", path, params=args)
            result.extend(data.get("items", []))
            if not data.get("has_more"):
                return result
            new = data.get("page_token")
            if not new or new == cursor:
                raise FeishuError("Feishu pagination did not advance")
            cursor = new

    def _operation(self, identity, method, path, body, *, check=None):
        op_id = stable_id("feishu", identity, method, body)
        previous = self.store.get("feishu_outbox", op_id)
        if not self.execute:
            return {"status": "dry_run", "operation": identity, "method": method, "body": body}
        if previous and previous["status"] == "delivered":
            return previous.get("result", {})
        if previous and previous["status"] in ("pending", "unknown"):
            found = check() if check else None
            if found is not None:
                self.store.put("feishu_outbox", op_id, dict(previous, status="delivered", result=found, reconciled_at=utc_now()))
                return found
            raise UnknownOutcome("An earlier Feishu write is unresolved; readback did not prove delivery")
        if any(value.get("identity") == identity and value.get("status") in ("pending", "unknown")
               for key, value in self.store.items("feishu_outbox") if key != op_id):
            raise UnknownOutcome("An earlier write for this identity is unresolved; changing its payload cannot bypass reconciliation")
        operation = {"id": op_id, "identity": identity, "status": "pending", "method": method,
                     "path": path, "body": body, "timestamp": utc_now()}
        self.store.put("feishu_outbox", op_id, operation)
        try:
            result = self._request(method, path, body=body)
        except UnknownOutcome:
            self.store.put("feishu_outbox", op_id, dict(operation, status="unknown"))
            raise
        except FeishuError:
            self.store.put("feishu_outbox", op_id, dict(operation, status="rejected"))
            raise
        # Persist the provider ID before readback, including across a restart.
        self.store.put("feishu_outbox", op_id, dict(operation, result=result))
        # A 2xx is insufficient: verify the state through a separate read.
        try:
            verified = check() if check else result
        except Exception:
            self.store.put("feishu_outbox", op_id, dict(operation, status="unknown", result=result))
            raise UnknownOutcome("Feishu write returned success but readback failed") from None
        if verified is None:
            self.store.put("feishu_outbox", op_id, dict(operation, status="unknown", result=result))
            raise UnknownOutcome("Feishu write could not be verified by readback")
        self.store.put("feishu_outbox", op_id, dict(operation, status="delivered", result=verified, verified_at=utc_now()))
        return verified

    def _base(self):
        if not self.app_token:
            raise FeishuError("Bitable is not configured; provision it or set INVEST_FEISHU_APP_TOKEN")
        return "/bitable/v1/apps/" + quote(self.app_token, safe="")

    def provision(self):
        preview = {"status": "dry_run", "app": "犀牛长期投资", "tables": SCHEMA}
        if not self.execute:
            return preview
        if not self.app_token:
            app_body = {"name": "犀牛长期投资", "time_zone": "Asia/Shanghai"}
            created = self._operation("create-investment-app", "POST", "/bitable/v1/apps", app_body)
            operation_id = stable_id("feishu", "create-investment-app", "POST", app_body)
            self.app_token = created.get("app", {}).get("app_token")
            if not self.app_token:
                old = self.store.get("feishu_outbox", operation_id)
                self.store.put("feishu_outbox", operation_id, dict(old, status="unknown"))
                raise UnknownOutcome("App creation outcome unknown; supply the app token from Feishu before reconciliation")
            self.store.put("feishu_meta", "app", {"app_token": self.app_token})
            try:
                self._request("GET", self._base())
            except FeishuError:
                old = self.store.get("feishu_outbox", operation_id)
                self.store.put("feishu_outbox", operation_id, dict(old, status="unknown"))
                raise UnknownOutcome("Created Bitable app has not passed readback; reconcile before continuing") from None
        tables = self._list(self._base() + "/tables")
        manifest = {}
        for name, names in SCHEMA.items():
            matches = [t for t in tables if t.get("name") == name]
            if len(matches) > 1:
                raise FeishuError("Duplicate managed tables require manual review: " + name)
            if not matches:
                def table_check(name=name):
                    found = [t for t in self._list(self._base() + "/tables") if t.get("name") == name]
                    if len(found) > 1:
                        raise FeishuError("Duplicate table after provisioning")
                    return {"table": found[0]} if found else None
                self._operation("table:" + name, "POST", self._base() + "/tables",
                                {"table": {"name": name, "default_view_name": "全部记录", "fields": [{"field_name": n, "type": 1} for n in names]}}, check=table_check)
                tables = self._list(self._base() + "/tables")
                matches = [t for t in tables if t.get("name") == name]
            table_id = matches[0]["table_id"]
            fields_path = self._base() + "/tables/" + table_id + "/fields"
            fields = self._list(fields_path)
            for field_name in names:
                existing = [f for f in fields if f.get("field_name") == field_name]
                if not existing:
                    def field_check(field_name=field_name, fields_path=fields_path):
                        found = [f for f in self._list(fields_path) if f.get("field_name") == field_name]
                        return {"field": found[0]} if len(found) == 1 else None
                    self._operation(f"field:{name}:{field_name}", "POST", fields_path, {"field_name": field_name, "type": 1}, check=field_check)
                    fields = self._list(fields_path)
                elif len(existing) != 1 or existing[0].get("type") != 1:
                    raise FeishuError("Managed field has incompatible type: " + name + "/" + field_name)
            manifest[name] = table_id
        self.store.put("feishu_meta", "tables", manifest)
        return {"status": "verified", "tables": list(manifest), "field_counts": {n: len(f) for n, f in SCHEMA.items()}}

    def tables(self):
        manifest = self.store.get("feishu_meta", "tables")
        if manifest:
            return manifest
        tables = self._list(self._base() + "/tables")
        manifest = {t["name"]: t["table_id"] for t in tables if t.get("name") in SCHEMA}
        if set(manifest) != set(SCHEMA):
            raise FeishuError("Investment tables are incomplete; run provision --execute")
        self.store.put("feishu_meta", "tables", manifest)
        return manifest

    def records(self, table):
        return self._list(self._base() + "/tables/" + self.tables()[table] + "/records")

    @staticmethod
    def _text(value):
        if isinstance(value, list):
            return "".join(str(v.get("text", "")) if isinstance(v, dict) else str(v) for v in value)
        return "" if value is None else str(value)

    def upsert(self, table, fields):
        unknown = set(fields) - set(SCHEMA[table])
        if unknown or not fields.get("系统标识"):
            raise ValueError("Record requires stable identity and declared Chinese fields")
        if not self.execute:
            return {"status": "dry_run", "table": table, "fields": fields}
        records = self.records(table)
        identity = "record:" + table + ":" + fields["系统标识"]
        # Recover a response lost after server commit, even if this projection's
        # newer timestamp/content changed while the request was in flight.
        for key, pending in self.store.items("feishu_outbox"):
            if pending.get("identity") != identity or pending.get("status") not in ("pending", "unknown"):
                continue
            expected = pending["body"]["fields"]
            delivered = [r for r in records if all(self._text(r.get("fields", {}).get(k)) == str(v) for k, v in expected.items())]
            if len(delivered) != 1:
                raise UnknownOutcome("Earlier record write is unresolved; readback must prove its exact content")
            self.store.put("feishu_outbox", key, dict(pending, status="delivered", result={"record": delivered[0]}, reconciled_at=utc_now()))
        matches = [r for r in records if self._text(r.get("fields", {}).get("系统标识")) == fields["系统标识"]]
        if len(matches) > 1:
            raise FeishuError("Duplicate record identities require manual review")
        base = self._base() + "/tables/" + self.tables()[table] + "/records"
        def check():
            found = [r for r in self.records(table) if self._text(r.get("fields", {}).get("系统标识")) == fields["系统标识"]]
            if len(found) > 1:
                raise FeishuError("Duplicate record identities after write")
            if found and all(self._text(found[0]["fields"].get(k)) == str(v) for k, v in fields.items()):
                return {"record": found[0]}
            return None
        verified = check()
        if verified:
            return verified
        method, path = ("PUT", base + "/" + matches[0]["record_id"]) if matches else ("POST", base)
        return self._operation(identity, method, path, {"fields": fields}, check=check)

    def poll_inputs(self):
        """Reads actual Bitable rows; never treats the notification webhook as input."""
        result = {"staged": [], "confirmed": [], "errors": [], "previews": []}
        for table in ("账户与资金", "成交录入"):
            for record in self.records(table):
                f = {k: self._text(v).strip() for k, v in record.get("fields", {}).items()}
                if table == "账户与资金" and (f.get("回撤硬限制设置") or f.get("持仓核对设置")) and f.get("设置确认状态") in ("待暂存", "确认设置"):
                    try:
                        settings = {}
                        if f.get("回撤硬限制设置"):
                            if f["回撤硬限制设置"] == "撤销回撤设置":
                                settings["drawdown_limit"] = None
                            else:
                                limit = decimal_value(f["回撤硬限制设置"])
                                if not 0 < limit < 1:
                                    raise ValueError("回撤限制须为0到1之间的小数；撤销时明确填写撤销回撤设置")
                                settings["drawdown_limit"] = str(limit)
                        if f.get("持仓核对设置"):
                            check = f["持仓核对设置"]
                            if check not in ("已核对", "已核对（可空仓）", "已核对(可空仓)", "未核对"):
                                raise ValueError("持仓核对设置须明确填写已核对（可空仓）或未核对")
                            settings["holdings_complete"] = check != "未核对"
                        self._control(result, "account_control", f.get("账户") or "real", settings, f["设置确认状态"], source=(table, record))
                    except (ValueError, FeishuError) as exc:
                        result["errors"].append({"record_id": record["record_id"], "error": str(exc)})
                        self._input_error(table, f.get("系统标识"), str(exc))
                entry_id = f.get("录入标识") if table == "账户与资金" else f.get("系统标识")
                if table == "账户与资金" and (not entry_id or not f.get("输入类型")):
                    continue
                status = f.get("确认状态", "")
                if status not in ("待暂存", "暂存", "确认", "已确认"):
                    continue
                raw_type = f.get("输入类型") if table == "账户与资金" else f.get("类型")
                if table == "成交录入" and not entry_id:
                    if not raw_type:
                        continue
                    entry_id = "feishu-" + stable_id(self.app_token, table, record["record_id"])
                    f["系统标识"] = entry_id
                    if self.execute:
                        try:
                            self._assign_input_identity(table, record["record_id"], entry_id)
                        except FeishuError as exc:
                            result["errors"].append({"record_id": record["record_id"], "error": str(exc)})
                            continue
                kind = TYPE_NAMES.get(raw_type, raw_type)
                entry = {"id": entry_id, "account": f.get("账户") or "real", "type": kind,
                         "currency": f.get("币种") or "USD", "timestamp": f.get("录入时间") if table == "账户与资金" else f.get("成交时间")}
                for field, key in (("股票", "symbol"), ("数量", "quantity"), ("成交价美元", "price"), ("费用美元", "fee"), ("金额美元", "amount")):
                    if f.get(field):
                        entry[key] = f[field]
                for field, key in (("推荐标识", "recommendation_id"), ("入场批次", "tranche_id")):
                    if f.get(field):
                        entry[key] = f[field]
                for field, key in (("拆股比例", "ratio"), ("持仓成本总额美元", "cost_basis"), ("首次建仓时间", "first_fill_at")):
                    if f.get(field):
                        entry[key] = f[field]
                if f.get("成本总额美元"):
                    entry["cost_basis"] = f["成本总额美元"]
                if table == "账户与资金":
                    entry["amount"] = f.get("录入金额美元")
                if kind == "correction":
                    replacement = dict(entry, id=f.get("原成交标识"),
                                       type=TYPE_NAMES.get(f.get("更正后类型"), f.get("更正后类型")))
                    entry = {"id": entry_id, "account": entry["account"], "type": "correction",
                             "timestamp": f.get("更正时间"), "currency": entry["currency"],
                             "corrects_id": f.get("原成交标识"), "reason": f.get("更正原因"),
                             "replacement": None if raw_type == "作废" else replacement}
                if not self.execute:
                    result["previews"].append({"entry": entry, "would_confirm": status in ("确认", "已确认")})
                    continue
                try:
                    staged = stage_entry(self.store, entry)
                    binding_key = stable_id("feishu-ledger-input", self.app_token, table, record["record_id"])
                    binding = self.store.get("feishu_ledger_input", binding_key) or {}
                    entry_hash = stable_id({k: v for k, v in staged.items() if k != "status"})
                    result["staged"].append(staged["id"])
                    if status in ("确认", "已确认"):
                        if binding.get("confirmed_entry_id") not in (None, entry_id) and (
                                binding.get("staged_entry_id") != entry_id or binding.get("staged_hash") != entry_hash):
                            raise ValueError("此行原成交确认已消费；新的录入标识须先暂存并核对，再明确确认")
                        with self.store.transaction():
                            confirmed = confirm_entry(self.store, entry_id)
                            self.store.put("feishu_ledger_input", binding_key,
                                {"confirmed_entry_id": entry_id, "confirmed_hash": entry_hash, "timestamp": utc_now()})
                        result["confirmed"].append(confirmed["id"])
                    else:
                        self.store.put("feishu_ledger_input", binding_key, dict(binding,
                            staged_entry_id=entry_id, staged_hash=entry_hash, timestamp=utc_now()))
                    # Acknowledgment updates only managed processing fields; never overwrite user input.
                    key = f.get("系统标识")
                    if key:
                        self.upsert(table, {"系统标识": key, "处理状态": "已确认" if status in ("确认", "已确认") else "已暂存", "错误说明": ""})
                except (ValueError, FeishuError) as exc:
                    # Exceptions are fixed local messages or sanitized transport messages.
                    result["errors"].append({"record_id": record["record_id"], "error": str(exc)})
                    self._input_error(table, f.get("系统标识"), str(exc))
        for record in self.records("投资计划"):
            f = {k: self._text(v).strip() for k, v in record.get("fields", {}).items()}
            if f.get("系统标识") and (f.get("单次投入设置美元") or f.get("入场批次设置")) and f.get("设置确认状态") in ("待暂存", "确认设置"):
                try:
                    if not self.store.get("plan", f["系统标识"]):
                        raise ValueError("Unknown investment plan; settings cannot create a plan")
                    prior = self.store.get("plan_control", f["系统标识"]) or {}
                    amount = decimal_value(f.get("单次投入设置美元") or prior.get("amount_usd") or "1000")
                    if not 1000 <= amount <= 3000:
                        raise ValueError("Each buy must be between USD 1000 and 3000")
                    tranche = f.get("入场批次设置") or prior.get("tranche_id") or "entry"
                    if len(tranche) > 64 or any(ord(c) < 32 for c in tranche):
                        raise ValueError("Entry tranche identifier must be at most 64 printable characters")
                    self._control(result, "plan_control", f["系统标识"], {"amount_usd": str(amount), "tranche_id": tranche}, f["设置确认状态"], source=("投资计划", record))
                except (ValueError, FeishuError) as exc:
                    result["errors"].append({"record_id": record["record_id"], "error": str(exc)})
                    self._input_error("投资计划", f.get("系统标识"), str(exc))
        return result

    def _assign_input_identity(self, table, record_id, identity):
        """Backfill only the managed ID on the original provider row, never POST a copy."""
        def check():
            found = [r for r in self.records(table) if r["record_id"] == record_id
                     and self._text(r.get("fields", {}).get("系统标识")) == identity]
            return {"record": found[0]} if len(found) == 1 else None
        path = self._base() + "/tables/" + self.tables()[table] + "/records/" + record_id
        return self._operation("record:" + table + ":" + identity, "PUT", path,
                               {"fields": {"系统标识": identity}}, check=check)

    def _input_error(self, table, key, message):
        if not self.execute or not key:
            return
        try:
            field = "设置错误说明" if table == "投资计划" else "错误说明"
            self.upsert(table, {"系统标识": key, field: message})
        except FeishuError:
            pass  # The durable outbox already preserves any uncertain acknowledgment.

    def _control(self, result, kind, key, values, status, *, source=None):
        if kind == "account_control" and key not in ("real", "paper"):
            raise ValueError("Account settings require real or paper")
        confirmed = status == "确认设置"
        # Independent user controls must not clear one another: acknowledging
        # complete holdings does not erase an already confirmed drawdown limit.
        base = self.store.get(kind, key) or {}
        payload = {k: v for k, v in base.items() if k not in ("timestamp", "confirmed")}
        payload.update(values, confirmed=confirmed)
        if not self.execute:
            result["previews"].append({"setting": kind, "id": key, **payload})
            return
        if source is not None:
            table, record = source
            state_key = stable_id("feishu-setting-confirmation", self.app_token, table, record["record_id"])
            identity = "setting-confirmation:" + state_key
            state = self.store.get("feishu_setting_confirmation", state_key) or {}
            input_hash = stable_id(kind, key, values)
            if not confirmed:
                self._write_control(kind + "_staged", key, payload)
                self.store.put("feishu_setting_confirmation", state_key, dict(state,
                    phase="staged", staged_hash=input_hash, timestamp=utc_now()))
                return
            # A new payload or explicit staging never bypasses an earlier unknown
            # provider write. Only the old unique receipt can prove that outcome.
            recovered_ack = False
            for op_key, operation in self.store.items("feishu_outbox"):
                if operation.get("identity") != identity or operation.get("status") not in ("pending", "unknown"):
                    continue
                expected = operation.get("body", {}).get("fields", {}).get("设置回执")
                found = [r for r in self.records(table) if r["record_id"] == record["record_id"]
                         and expected and self._text(r.get("fields", {}).get("设置回执")) == expected]
                if len(found) != 1:
                    raise UnknownOutcome("上次设置回执结果未知；须先核实原回执，不能用新数值或新确认绕过")
                self.store.put("feishu_outbox", op_key, dict(operation, status="delivered",
                    result={"record": found[0]}, reconciled_at=utc_now()))
                recovered_ack = True
            if recovered_ack:
                # The outer row snapshot may predate the recovered server ack.
                # Never treat its old confirm text as authority for a new value.
                self.store.put("feishu_setting_confirmation", state_key, dict(state, phase="applied", acknowledged_at=utc_now()))
                return
            prior_op = self.store.get("feishu_outbox", state.get("operation_id", ""))
            if state.get("phase") == "consumed" and prior_op and prior_op.get("status") == "delivered":
                self.store.put("feishu_setting_confirmation", state_key, dict(state, phase="applied", acknowledged_at=utc_now()))
                return
            if state.get("phase") == "consumed":
                if state.get("input_hash") != input_hash:
                    raise ValueError("原设置确认已消费；编辑数值后请先待暂存，再明确确认设置")
                self._ack_control(table, record["record_id"], state_key, state)
                return
            if state.get("phase") == "staged" and state.get("staged_hash") != input_hash:
                raise ValueError("设置内容已在暂存后变化；请重新待暂存并核对后确认设置")
            generation = state.get("generation", 0) + 1
            receipt = stable_id(state_key, generation, input_hash)
            body = {"fields": {"设置确认状态": "已应用", "设置回执": receipt}}
            consumed = {"phase": "consumed", "generation": generation, "input_hash": input_hash,
                        "body": body, "timestamp": utc_now(),
                        "operation_id": stable_id("feishu", identity, "PUT", body)}
            # Apply once and consume before external acknowledgement. A lost ack
            # cannot turn this old confirmation into authority for a new value.
            with self.store.transaction():
                self._write_control(kind, key, payload)
                self.store.put("feishu_setting_confirmation", state_key, consumed)
            self._ack_control(table, record["record_id"], state_key, consumed)
            result.setdefault("settings_applied", []).append({"kind": kind, "id": key})
            return
        namespace = kind if confirmed else kind + "_staged"
        self._write_control(namespace, key, payload)

    def _write_control(self, namespace, key, payload):
        previous = self.store.get(namespace, key)
        if previous and all(previous.get(k) == v for k, v in payload.items()):
            return
        self.store.put(namespace, key, dict(payload, timestamp=utc_now()))

    def _ack_control(self, table, record_id, state_key, consumed):
        path = self._base() + "/tables/" + self.tables()[table] + "/records/" + record_id
        expected = consumed["body"]["fields"]["设置回执"]
        def check():
            found = [r for r in self.records(table) if r["record_id"] == record_id
                     and self._text(r.get("fields", {}).get("设置回执")) == expected]
            return {"record": found[0]} if len(found) == 1 else None
        self._operation("setting-confirmation:" + state_key, "PUT", path, consumed["body"], check=check)
        self.store.put("feishu_setting_confirmation", state_key, dict(consumed, phase="applied", acknowledged_at=utc_now()))

    def notify(self, notification_id, text):
        """Human-facing text; delivery IDs stay in the durable outbox."""
        if self.execute:
            for _, operation in self.store.items("feishu_outbox"):
                if operation.get("identity") != "notice:" + notification_id:
                    continue
                if operation.get("status") in ("delivered", "acknowledged"):
                    return operation.get("result", {})
                if operation.get("route") == "webhook" and operation.get("status") in ("pending", "unknown"):
                    raise UnknownOutcome("Webhook outcome is unresolved and has no API readback; automatic resend is fenced")
                if operation.get("status") in ("pending", "unknown"):
                    found = self._notice_readback(operation)
                    if found is not None:
                        self.store.put("feishu_outbox", operation["id"], dict(operation, status="delivered", result=found, reconciled_at=utc_now()))
                        return found
                    raise UnknownOutcome("Message outcome is unresolved; no matching provider message ID was verified")
        if not self.config.feishu_chat_id:
            if self.config.feishu_webhook_url:
                return self._notify_webhook(notification_id, text)
            return {"status": "disabled", "reason": "chat_or_explicit_webhook_not_configured"}
        path = "/im/v1/messages?receive_id_type=chat_id"
        body = {"receive_id": self.config.feishu_chat_id, "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": stable_id("notice", notification_id)[:32]}
        def check():
            operation = self.store.get("feishu_outbox", stable_id("feishu", "notice:" + notification_id, "POST", body))
            return self._notice_readback(operation or {})
        return self._operation("notice:" + notification_id, "POST", path, body, check=check)

    def _notice_readback(self, operation):
        message_id = (operation.get("result") or {}).get("message_id")
        expected = operation.get("body", {}).get("content")
        if message_id and expected:
            data = self._request("GET", "/im/v1/messages/" + quote(message_id, safe=""))
            for msg in data.get("items", []):
                if msg.get("message_id") == message_id:
                    try:
                        matches = json.loads(msg.get("body", {}).get("content", "")) == json.loads(expected)
                    except (ValueError, TypeError):
                        matches = False
                    if matches:
                        return {"message_id": message_id}
            return None
        # Legacy messages can still be reconciled by their old visible marker.
        # New messages without a provider ID remain fenced; identical text is
        # never sufficient evidence that this particular send was delivered.
        marker = "[投资回执:" + operation.get("identity", "").removeprefix("notice:") + "]"
        if expected and marker in expected:
            chat = operation.get("body", {}).get("receive_id")
            messages = self._list("/im/v1/messages", params={"container_id_type": "chat", "container_id": chat})
            matches = [msg for msg in messages if marker in msg.get("body", {}).get("content", "")]
            if len(matches) == 1:
                return {"message_id": matches[0]["message_id"]}
        return None

    def _notify_webhook(self, notification_id, text):
        """Custom bots acknowledge acceptance; they cannot verify chat readback."""
        identity = "notice:" + notification_id
        body = {"msg_type": "text", "content": {"text": text}}
        if not self.execute:
            return {"status": "dry_run", "operation": identity, "route": "webhook",
                    "verification": "provider_ack_only", "readback_verified": False, "body": body}
        if any(v.get("identity") == identity and v.get("status") in ("pending", "unknown")
               for _, v in self.store.items("feishu_outbox")):
            raise UnknownOutcome("An earlier notice outcome is unknown; switching notification routes cannot authorize resend")
        key = stable_id("feishu-webhook-notice", notification_id)
        operation = {"id": key, "identity": identity, "route": "webhook", "status": "pending",
                     "method": "POST", "path": "custom-bot-webhook", "body": body,
                     "target_hash": stable_id(self.config.feishu_webhook_url), "timestamp": utc_now()}
        # Never persist the secret-bearing URL, signing secret, or signature.
        self.store.put("feishu_outbox", key, operation)
        payload = dict(body)
        if self.config.feishu_webhook_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac.new(
                f"{timestamp}\n{self.config.feishu_webhook_secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
        try:
            response = self.http.request("POST", self.config.feishu_webhook_url, json=payload,
                                         timeout=(10, 30), allow_redirects=False)
        except Exception:
            self.store.put("feishu_outbox", key, dict(operation, status="unknown"))
            raise UnknownOutcome("Webhook transport outcome unknown; no readback endpoint exists and no automatic resend is allowed") from None
        code = None
        try:
            returned = response.json()
            if isinstance(returned, dict):
                code = returned.get("code", returned.get("StatusCode"))
        except Exception:
            pass
        definitive_http_rejection = response.status_code in (400, 401, 403, 404, 413, 422, 429)
        definitive_business_rejection = response.status_code == 200 and isinstance(code, int) and not isinstance(code, bool) and code != 0
        if definitive_http_rejection or definitive_business_rejection:
            self.store.put("feishu_outbox", key, dict(operation, status="rejected", http_status=response.status_code,
                provider_code=code if isinstance(code, int) and not isinstance(code, bool) else None))
            raise FeishuError("Webhook provider rejected the notice; credentials and response body omitted")
        if response.status_code != 200 or not isinstance(code, int) or isinstance(code, bool) or code != 0:
            self.store.put("feishu_outbox", key, dict(operation, status="unknown", http_status=response.status_code))
            raise UnknownOutcome("Webhook response did not prove provider acceptance; no readback or automatic resend is available")
        result = {"status": "provider_ack", "route": "webhook", "notification_id": notification_id,
                  "verification": "provider_ack_only", "readback_verified": False, "provider_code": 0}
        self.store.put("feishu_outbox", key, dict(operation, status="acknowledged", result=result,
            acknowledged_at=utc_now(), verification="provider_ack_only", readback_verified=False))
        return result

    def reconcile(self):
        resolved, unknown = [], []
        for key, operation in self.store.items("feishu_outbox"):
            if operation["status"] not in ("pending", "unknown"):
                continue
            identity = operation["identity"]
            try:
                if operation.get("route") == "webhook":
                    unknown.append({"id": key, "operation": identity, "reason": "webhook_has_no_readback_manual_verification_required"})
                    continue
                if identity.startswith("setting-confirmation:"):
                    parts = operation["path"].split("/")
                    table_id, record_id = parts[-3], parts[-1]
                    table = next((name for name, tid in self.tables().items() if tid == table_id), None)
                    expected = operation.get("body", {}).get("fields", {}).get("设置回执")
                    found = [r for r in self.records(table) if r["record_id"] == record_id
                             and expected and self._text(r.get("fields", {}).get("设置回执")) == expected] if table else []
                    if len(found) == 1 and self.execute:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"record": found[0]}, reconciled_at=utc_now()))
                elif identity.startswith("record:"):
                    _, table, _ = identity.split(":", 2)
                    fields = operation["body"]["fields"]
                    found = [r for r in self.records(table) if all(self._text(r.get("fields", {}).get(k)) == str(v) for k, v in fields.items())]
                    if len(found) == 1 and self.execute:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"record": found[0]}, reconciled_at=utc_now()))
                elif identity.startswith("notice:"):
                    text = json.loads(operation["body"]["content"])["text"].rsplit("\n[投资回执:", 1)[0]
                    self.notify(identity.removeprefix("notice:"), text)
                elif identity.startswith("table:"):
                    found = [t for t in self._list(self._base() + "/tables") if t.get("name") == identity.removeprefix("table:")]
                    if len(found) == 1 and self.execute:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"table": found[0]}, reconciled_at=utc_now()))
                elif identity.startswith("field:"):
                    _, table, name = identity.split(":", 2)
                    found = [f for f in self._list(self._base() + "/tables/" + self.tables()[table] + "/fields") if f.get("field_name") == name]
                    if len(found) == 1 and self.execute:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"field": found[0]}, reconciled_at=utc_now()))
                elif identity == "create-investment-app" and self.app_token:
                    self._request("GET", self._base())
                    if self.execute:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"app": {"app_token": self.app_token}}, reconciled_at=utc_now()))
                else:
                    unknown.append({"id": key, "operation": identity})
                    continue
                current = self.store.get("feishu_outbox", key)
                if self.execute and current and current["status"] in ("pending", "unknown") and identity.startswith("record:"):
                    records = self.records(identity.split(":", 2)[1])
                    fields = operation["body"]["fields"]
                    found = [r for r in records if all(self._text(r.get("fields", {}).get(k)) == str(v) for k, v in fields.items())]
                    if len(found) == 1:
                        self.store.put("feishu_outbox", key, dict(operation, status="delivered", result={"record": found[0]}, reconciled_at=utc_now()))
                current = self.store.get("feishu_outbox", key)
                (resolved if current and current["status"] == "delivered" else unknown).append({"id": key, "operation": identity})
            except FeishuError:
                unknown.append({"id": key, "operation": identity})
        return {"resolved": resolved, "unknown": unknown, "execute": self.execute}
