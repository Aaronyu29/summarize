"""Investment CLI. External writes and real-ledger confirmation are opt-in."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .store import Store
from .ledger import import_csv, confirm_entry, portfolio, correct_entry
from .feishu import Feishu
from .runner import Runner, TradingCalendar, status
from .market import MarketData
from .common import utc_now, stable_id
from .feishu import SCHEMA
from .research import register_external_evidence, bind_instrument_identity
from .budget import budget_status, read_budget_status, validate_limits, write_limits


def parser():
    p = argparse.ArgumentParser(prog="python -m rhino_invest", description="犀牛长期投资：人工实盘账本与独立模拟")
    p.add_argument("--private-config", help="Explicit private JSON configuration; never printed")
    p.add_argument("--database", help="Investment SQLite path, separate from video archive")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("diagnostics", "provision", "run-once", "serve", "status", "budget", "reconcile", "portfolio", "import-csv", "confirm", "correct", "import-evidence", "bind-instrument"):
        command = sub.add_parser(name)
        if name in ("provision", "run-once", "serve", "budget", "reconcile", "import-csv", "confirm", "correct", "import-evidence", "bind-instrument"):
            command.add_argument("--execute", action="store_true", help="Perform authorized writes; default is preview")
        if name == "budget":
            command.add_argument("--calls", type=int, help="Daily call ceiling; does not reset usage")
            command.add_argument("--cost-usd", help="Daily estimated reservation ceiling, not actual provider cost")
        if name == "diagnostics":
            command.add_argument("--live", action="store_true", help="Read official quotes, exchange calendar and Bitable schema")
        if name in ("portfolio", "import-csv"):
            command.add_argument("--account", choices=("real", "paper"), default="real")
        if name == "import-csv":
            command.add_argument("path")
        if name == "confirm":
            command.add_argument("ids", nargs="+", help="Explicit staged identifiers; no confirm-all")
        if name == "correct":
            command.add_argument("original_id")
            command.add_argument("--id", required=True, help="Stable correction identifier")
            command.add_argument("--timestamp", required=True, help="Correction time with timezone; stable across retries")
            command.add_argument("--reason", default="manual correction")
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument("--replacement-csv", help="One corrected row; original audit event is retained")
            choice.add_argument("--void", action="store_true")
        if name == "import-evidence":
            command.add_argument("path", help="Local reviewed primary-source text/JSON; never fetched from a URL")
            command.add_argument("--source-url", required=True, help="Primary-source HTTPS citation")
            command.add_argument("--as-of", required=True, help="Evidence date/time with timezone")
            command.add_argument("--reviewed-by", required=True, help="Human reviewer attesting to this source")
        if name == "bind-instrument":
            command.add_argument("symbol", help="Bare US ticker")
            command.add_argument("metadata", help="Local JSON with reviewed ADR/currency/unit facts")
            command.add_argument("--evidence-id", action="append", required=True, help="Imported source identifier; repeat for multiple sources")
            command.add_argument("--reviewed-by", required=True)
            command.add_argument("--reviewed-at", required=True, help="Human review date/time with timezone")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = Config.load(args.private_config)
        if args.database:
            config.database = args.database
        if args.command == 'budget':
            result = read_budget_status(config.database, config.model)
            if args.calls is not None or args.cost_usd is not None:
                limits = validate_limits({'max_calls_per_day': args.calls, 'max_cost_usd_per_day': args.cost_usd})
                if not config.model.get('budget_file'):
                    raise ValueError('Configure INVEST_MODEL_BUDGET_FILE before changing live limits')
                if args.execute:
                    write_limits(config.model['budget_file'], limits,
                        protected_paths=(config.database, config.archive_database, args.private_config))
                    result = read_budget_status(config.database, config.model)
                result.update(proposed_limits=limits, applied=args.execute, usage_reset=False, restart_required=False)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        with Store(config.database) as store:
            execute = getattr(args, "execute", False)
            feishu = Feishu(store, config, execute=execute)
            if args.command == "diagnostics":
                result = config.diagnostics()
                snapshot = portfolio(store, "real")
                result["ledger_gates"] = {k: snapshot.get(k) for k in ("cash_complete", "cost_complete", "fees_complete")}
                result["unresolved_feishu_writes"] = status(store)["pending_feishu"]
                if args.live:
                    result["calendar"] = TradingCalendar().state(utc_now())
                    result["quotes"] = MarketData().snapshots(config.symbols) if config.symbols else []
                    if feishu.app_token:
                        manifest = feishu.tables()
                        result["feishu_schema"] = {}
                        for table, table_id in manifest.items():
                            fields = feishu._list(feishu._base() + "/tables/" + table_id + "/fields")
                            actual = {f["field_name"]: f["type"] for f in fields}
                            result["feishu_schema"][table] = {"missing_fields": [n for n in SCHEMA[table] if n not in actual],
                                "incompatible_fields": [n for n in SCHEMA[table] if n in actual and actual[n] != 1]}
                result["runtime_acceptance"] = "Live permissions, official quote fields, paid model and delivery need separate verification"
            elif args.command == "provision":
                result = feishu.provision()
            elif args.command == "reconcile":
                result = feishu.reconcile()
            elif args.command == "status":
                result = status(store)
                result['model_budget'] = budget_status(store, config.model)
            elif args.command == "portfolio":
                result = portfolio(store, args.account)
            elif args.command == "import-csv":
                if execute:
                    result = import_csv(store, args.path, account=args.account)
                    result["status"] = "staged_only"
                else:
                    with Store(":memory:") as preview:
                        result = import_csv(preview, args.path, account=args.account)
                    result["status"] = "dry_run_not_staged"
            elif args.command == "confirm":
                if not execute:
                    result = {"status": "dry_run", "would_confirm": [store.get("ledger_staged", key) for key in args.ids]}
                    if any(v is None for v in result["would_confirm"]):
                        raise ValueError("A requested staged identifier was not found")
                else:
                    # Single batch transaction avoids partial confirmation on validation failure.
                    with store.transaction():
                        result = {"status": "confirmed", "entries": [confirm_entry(store, key) for key in args.ids]}
            elif args.command == "correct":
                original = next((e for e in store.events("ledger") if e["id"] == args.original_id), None)
                if not original:
                    raise ValueError("Original confirmed entry was not found")
                replacement = None
                if args.replacement_csv:
                    with Store(":memory:") as preview:
                        imported = import_csv(preview, args.replacement_csv, account=original["account"])
                    if imported["errors"] or imported["count"] != 1:
                        raise ValueError("Replacement CSV must contain exactly one valid row in the original account")
                    replacement = imported["staged"][0]
                if not execute:
                    result = {"status": "dry_run", "original_id": args.original_id, "replacement": replacement,
                              "correction_id": args.id, "timestamp": args.timestamp}
                else:
                    result = correct_entry(store, args.original_id, replacement, id=args.id,
                        timestamp=args.timestamp, reason=args.reason)
            elif args.command == "import-evidence":
                kwargs = {"source_url": args.source_url, "as_of": args.as_of, "reviewed_by": args.reviewed_by}
                if execute:
                    evidence_id = register_external_evidence(store, args.path, **kwargs)
                else:
                    with Store(":memory:") as preview:
                        evidence_id = register_external_evidence(preview, args.path, **kwargs)
                result = {"status": "imported" if execute else "dry_run_not_imported", "evidence_id": evidence_id,
                          "source_fetched": False, "human_review_required": True}
            elif args.command == "bind-instrument":
                metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise ValueError("Instrument metadata must be a JSON object")
                kwargs = {"evidence_refs": args.evidence_id, "reviewed_by": args.reviewed_by, "reviewed_at": args.reviewed_at}
                if execute:
                    with store.transaction():
                        identity = bind_instrument_identity(store, args.symbol, metadata, **kwargs)
                        key = stable_id("instrument_identity_change", identity["id"])
                        if store.get("review_trigger", key) is None:
                            store.put("review_trigger", key, {"id": key, "symbol": identity["symbol"],
                                "kind": "instrument_identity_change", "timestamp": utc_now(),
                                "evidence": identity, "processed": False})
                else:
                    with Store(":memory:") as preview:
                        for evidence_id in args.evidence_id:
                            evidence = store.get("external_evidence", evidence_id)
                            if evidence:
                                preview.put("external_evidence", evidence_id, evidence, immutable=True)
                        identity = bind_instrument_identity(preview, args.symbol, metadata, **kwargs)
                result = {"status": "bound_review_queued" if execute else "dry_run_not_bound",
                          "identity": identity, "paid_model_called": False, "plan_activated": False}
            elif args.command == "run-once":
                result = Runner(store, config, feishu=feishu, execute=execute).run_once()
            elif args.command == "serve":
                Runner(store, config, feishu=feishu, execute=execute).serve()
                result = {"status": "stopped"}
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
    except Exception as exc:
        # Never stringify arbitrary HTTP/SDK errors, which can contain credentials.
        safe = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({"status": "error", "error": safe}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
