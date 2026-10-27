"""Explicit investment-only settings. Importing this module has no side effects."""
from __future__ import annotations

import json
import os
import importlib.util
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from .common import parse_time
from .budget import load_limits


@dataclass
class Config:
    database: str = "research-data/investment/investment.sqlite"
    archive_database: str | None = None
    research_since: str | None = None
    symbols: tuple[str, ...] = ()
    interval_seconds: int = 60
    notifications_enabled: bool = True
    system_remediation_enabled: bool = False
    feishu_app_id: str | None = field(default=None, repr=False)
    feishu_app_secret: str | None = field(default=None, repr=False)
    feishu_app_token: str | None = field(default=None, repr=False)
    feishu_chat_id: str | None = field(default=None, repr=False)
    feishu_webhook_url: str | None = field(default=None, repr=False)
    feishu_webhook_secret: str | None = field(default=None, repr=False)
    feishu_reuse_lark_webhook: bool = False
    feishu_webhook_source: str | None = field(default=None, repr=False)
    feishu_base_url: str = "https://open.feishu.cn/open-apis"
    model: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if isinstance(self.system_remediation_enabled, str):
            value = self.system_remediation_enabled.strip().lower()
            if value not in {'true','false','1','0','yes','no','on','off'}:
                raise ValueError('INVEST_SYSTEM_REMEDIATION_ENABLED must be an explicit boolean')
            self.system_remediation_enabled = value in {'true','1','yes','on'}
        elif not isinstance(self.system_remediation_enabled, bool):
            raise ValueError('system_remediation_enabled must be a boolean')
        if isinstance(self.notifications_enabled, str):
            value = self.notifications_enabled.strip().lower()
            if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("INVEST_NOTIFICATIONS_ENABLED must be an explicit boolean")
            self.notifications_enabled = value in {"true", "1", "yes", "on"}
        elif not isinstance(self.notifications_enabled, bool):
            raise ValueError("notifications_enabled must be a boolean")
        if self.research_since is not None:
            self.research_since = parse_time(self.research_since).isoformat()
        if self.feishu_webhook_url:
            try:
                parsed = urlsplit(self.feishu_webhook_url)
                valid = (parsed.scheme == "https" and parsed.hostname in {"open.feishu.cn", "open.larksuite.com"}
                         and not parsed.username and not parsed.password and not parsed.port
                         and re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", parsed.path)
                         and not parsed.query and not parsed.fragment)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("Configure a valid Feishu/Lark custom bot webhook; URL omitted")

    @classmethod
    def load(cls, private_file=None, environ=None):
        env = os.environ if environ is None else environ
        raw = {}
        if private_file:
            path = Path(private_file)
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Private settings must be a JSON object")
        mapping = {
            "database": "INVEST_DATABASE", "archive_database": "INVEST_ARCHIVE_DATABASE",
            "research_since": "INVEST_RESEARCH_SINCE",
            "symbols": "INVEST_SYMBOLS", "interval_seconds": "INVEST_INTERVAL_SECONDS",
            "notifications_enabled": "INVEST_NOTIFICATIONS_ENABLED",
            "system_remediation_enabled": "INVEST_SYSTEM_REMEDIATION_ENABLED",
            "feishu_app_id": "INVEST_FEISHU_APP_ID", "feishu_app_secret": "INVEST_FEISHU_APP_SECRET",
            "feishu_app_token": "INVEST_FEISHU_APP_TOKEN", "feishu_chat_id": "INVEST_FEISHU_CHAT_ID",
            "feishu_webhook_url": "INVEST_FEISHU_WEBHOOK_URL", "feishu_webhook_secret": "INVEST_FEISHU_WEBHOOK_SECRET",
        }
        for key, name in mapping.items():
            if env.get(name):
                raw[key] = env[name]
        reuse = env.get("INVEST_REUSE_LARK_WEBHOOK", raw.get("feishu_reuse_lark_webhook", False))
        if isinstance(reuse, str):
            if reuse.strip().lower() not in ("", "0", "1", "false", "true", "no", "yes", "off", "on"):
                raise ValueError("INVEST_REUSE_LARK_WEBHOOK must be an explicit boolean")
            reuse = reuse.strip().lower() in ("1", "true", "yes", "on")
        elif not isinstance(reuse, bool):
            raise ValueError("feishu_reuse_lark_webhook must be a boolean")
        raw["feishu_reuse_lark_webhook"] = reuse
        if raw.get("feishu_webhook_url"):
            raw["feishu_webhook_source"] = "investment"
        elif reuse and (env.get("LARK_WEBHOOK_URL") or env.get("LARK_WEBHOOK")):
            raw["feishu_webhook_url"] = env.get("LARK_WEBHOOK_URL") or env["LARK_WEBHOOK"]
            raw["feishu_webhook_source"] = "legacy_explicit"
            if not raw.get("feishu_webhook_secret") and env.get("LARK_WEBHOOK_SECRET"):
                raw["feishu_webhook_secret"] = env["LARK_WEBHOOK_SECRET"]
        raw["symbols"] = tuple(str(s).strip().upper().removesuffix(".US") for s in
                               (raw.get("symbols", "").split(",") if isinstance(raw.get("symbols", ""), str)
                                else raw["symbols"]) if str(s).strip())
        raw["interval_seconds"] = int(raw.get("interval_seconds", 60))
        if raw["interval_seconds"] < 60:
            raise ValueError("Quote polling interval must be at least 60 seconds")
        model = dict(raw.get("model", {}))
        if env.get("INVEST_MODEL_ENABLED") is not None:
            model["enabled"] = env["INVEST_MODEL_ENABLED"].lower() in ("1", "true", "yes")
        for key, name in (("model", "INVEST_MODEL_NAME"), ("base_url", "INVEST_MODEL_BASE_URL"),
                          ("api_key_env", "INVEST_MODEL_API_KEY_ENV"),
                          ("max_calls_per_day", "INVEST_MODEL_MAX_CALLS_PER_DAY"),
                          ("budget_file", "INVEST_MODEL_BUDGET_FILE"),
                          ("auto_budget_policy_file", "INVEST_MODEL_AUTO_BUDGET_POLICY_FILE"),
                          ("max_tokens", "INVEST_MODEL_MAX_TOKENS"),
                          ("thinking", "INVEST_MODEL_THINKING"),
                          ("reasoning_effort", "INVEST_MODEL_REASONING_EFFORT"),
                          ("configuration_revision", "INVEST_MODEL_CONFIGURATION_REVISION"),
                          ("max_cost_usd_per_day", "INVEST_MODEL_MAX_COST_USD_PER_DAY"),
                          ("max_cost_usd_per_call", "INVEST_MODEL_MAX_COST_USD_PER_CALL")):
            if env.get(name):
                model[key] = env[name]
        model.setdefault("enabled", False)
        raw["model"] = model
        allowed = cls.__dataclass_fields__
        unexpected = set(raw) - set(allowed)
        if unexpected:
            raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unexpected)))
        return cls(**raw)

    def diagnostics(self):
        """Never include credentials, endpoints with user info, or private file content."""
        model_ready = bool(self.model.get("enabled") and self.model.get("base_url") and
                           self.model.get("model") and self.model.get("api_key_env") and
                           os.environ.get(self.model.get("api_key_env", "")))
        try:
            limits = load_limits(self.model)
            model_ready = model_ready and limits['max_calls_per_day'] > 0 and float(limits['max_cost_usd_per_day']) > 0 \
                and float(self.model.get('max_cost_usd_per_call', 0)) > 0
        except (TypeError, ValueError):
            model_ready = False
        return {
            "database": self.database, "archive_configured": bool(self.archive_database),
            "research_since": self.research_since,
            "symbols": list(self.symbols), "interval_seconds": self.interval_seconds,
            "notifications_enabled": self.notifications_enabled,
            "feishu_credentials_configured": bool(self.feishu_app_id and self.feishu_app_secret),
            "feishu_bitable_configured": bool(self.feishu_app_token),
            "feishu_chat_configured": bool(self.feishu_chat_id),
            "feishu_webhook_configured": bool(self.feishu_webhook_url),
            "feishu_webhook_source": self.feishu_webhook_source,
            "feishu_notification_route": ("chat_readback" if self.feishu_chat_id else
                "webhook_provider_ack_only" if self.feishu_webhook_url else "disabled"),
            "feishu_webhook_signing_configured": bool(self.feishu_webhook_secret),
            "model_enabled": bool(self.model.get("enabled")),
            "model_ready": model_ready,
            "official_market_adapter_installed": importlib.util.find_spec("stock_alert") is not None,
            "trading_calendar_installed": importlib.util.find_spec("exchange_calendars") is not None,
            "real_trading": "manual_only", "target_total_usd": "20000",
            "each_buy_usd": ["1000", "3000"], "drawdown_limit": None,
        }
