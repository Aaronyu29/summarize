"""Readable, evidence-aware Feishu projections, separate from accounting truth."""
from __future__ import annotations

import json
import re
from datetime import timedelta, timezone
from .common import stable_id, utc_now, parse_time, decimal_value


ACTIONS = {"buy": "首次买入", "add": "继续加仓", "hold": "继续持有", "reduce": "分批减仓", "exit": "全部退出", "watch": "继续观察"}
REASONS = {
    "actual_holdings_not_reconciled": "实际持仓尚未核对，不能把未录入的持仓当作空仓",
    "outside_entry_zone": "当前价格未进入买入区间", "verified_entry_zone_and_reserved_cash": "价格进入已核实区间，已预留规划资金",
    "actual_account_cash_cost_or_fees_incomplete": "实际现金、持仓成本或费用尚未补齐", "account_cash_currency_unverified": "账户现金或币种尚未核实",
    "insufficient_unreserved_actual_cash": "扣除其他规划预留后，实际现金不足", "verified_thesis_invalidation": "已核实投资逻辑失效",
    "confirmed_price_stop": "价格止损已按约定方式确认", "take_profit_schedule_reached": "达到分批止盈条件",
    "scheduled_thesis_review_due": "已到投资逻辑复核日期", "investment_horizon_expired_requires_new_thesis": "投资期限已到，需要重新论证",
    "ledger_input_not_current": "飞书账本输入读取失败，暂停新增建议", "official_source_required": "缺少官方行情来源",
    "quote_not_current": "行情当前不可用或存在延迟", "regular_session_required": "当前不是常规交易时段",
    "quote_timestamp_not_fresh": "行情时间已过期", "currency_unverified": "行情币种尚未核实",
    "verified_next_regular_close_required": "缺少交易日历确认的下次收盘时间", "plan_not_active": "投资计划尚未通过审核",
    "entry_conditions_unverified": "买入条件尚未核实", "portfolio_drawdown_not_evaluable": "已设置回撤上限，但缺少可比历史净值",
    "portfolio_drawdown_breached": "组合已达到设置的回撤上限", "budget_below_one_share_plus_fee": "预算不足以购买一整股并支付费用",
    "reported_holding_quantity_unknown_requires_risk_check": "你提供的持仓股数尚未核对，请检查实际持仓风险",
    "position_structure_changed_requires_take_profit_review": "持仓结构已变化，分批止盈规则需重新核对",
    "announced_corporate_action_requires_price_unit_review": "已到拆股等公司行动生效日，价格口径与持仓股数需重新核对",
}


def human_reasons(reasons):
    result = []
    for reason in reasons or []:
        if isinstance(reason, str) and reason.startswith("tranche_"):
            result.append("本批次已处理或过期；再次加仓须确认新的入场批次")
        else:
            result.append(REASONS.get(str(reason), "需核查：" + readable(reason)))
    return "；".join(result)


def readable(value):
    if value is None:
        return "未知／未配置"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def beijing_time(value):
    try:
        return parse_time(value).astimezone(timezone(timedelta(hours=8))).strftime("%m月%d日 %H:%M") + "（北京时间）"
    except (TypeError, ValueError):
        return "时间待核对"


def _number(value, *, money=False):
    try:
        number = decimal_value(value)
        return ("$" if money else "") + f"{number:,.2f}"
    except (TypeError, ValueError):
        return "待补"


def _symbol(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", value) else "该股票"


def _notification_reason(reasons):
    values = []
    for reason in reasons or []:
        text = REASONS.get(reason) if isinstance(reason, str) else None
        if text and text not in values:
            values.append(text)
    return "；".join(values[:2]) or "资料或交易条件尚待核实"


def actionable_notice(recommendation, current_plans, *, now=None):
    """Only a still-current approved plan can expose actionable price levels."""
    plan = next((p for p in current_plans if p.get("id") == recommendation.get("plan_id")), None)
    if now is not None:
        try:
            if parse_time(recommendation.get("expires_at")) <= parse_time(now) or parse_time(plan.get("next_review_at")) <= parse_time(now):
                return False
        except (TypeError, ValueError, AttributeError):
            return False
    return bool(plan and (plan.get("research_assessment") or {}).get("status") != "approved_conditional"
                and plan.get("status") == "active" and plan.get("revision") == recommendation.get("revision")
                and recommendation.get("status") == "actionable"
                and recommendation.get("action") in {"buy", "add", "reduce", "exit"})


def notification_text(report, *, current_plans=(), recommendations=(), watch_event=None, now=None, table_url=None):
    """Allowlisted short Chinese delivery, including for legacy audit-only reports."""
    stamp = beijing_time(report.get("timestamp"))
    if report.get("kind") == "风险提示":
        info = report.get("risk_info") or {}
        symbol = _symbol(info.get("symbol"))
        account = "模拟账户" if report.get("account") == "paper" else "真实账户"
        reasons = info.get("reasons", [])
        cause = "原投资逻辑已被新证据否定" if "verified_thesis_invalidation" in reasons else "已确认达到原计划止损条件"
        lines = [f"【持仓风险·{account}】{stamp}", symbol + "：" + cause + "。"]
        if info.get("quantity") is not None:
            lines.append("已确认持仓：" + _number(info["quantity"]) + "股。")
        else:
            lines.append("你提供的持仓市值：" + _number(info.get("reported_market_value"), money=True) + "；股数待核对。")
        if info.get("price_plan_verified") is True and "confirmed_price_stop" in reasons:
            stop = info.get("stop") or {}
            mode = {"daily_close": "常规交易日收盘确认", "weekly_close": "本周最后交易日收盘确认", "intraday": "常规时段盘中确认"}.get(stop.get("confirmation"), "确认方式待核对")
            lines.append("原已审核止损：" + _number(stop.get("price"), money=True) + "，" + mode + "。")
        if not info.get("quote_executable"):
            lines.append("当前行情不可用于执行，请核对券商报价和实际持仓。")
        elif info.get("quantity") is None:
            lines.append("请先核对股数，再检查券商报价和持仓风险。")
        elif info.get("ledger_current") is False:
            lines.append("账本读取暂未完成，请核对实际持仓后检查风险。")
        elif info.get("advice_executable") is not True:
            lines.append("原计划需要复核，请人工检查持仓风险。")
        else:
            lines.append("请核对券商报价和实际持仓，人工检查退出风险。")
        lines.append("这是风险核对提醒，不是下单指令。")
        return "\n".join(lines)
    if report.get("kind") == "价格观察":
        event = watch_event or {}
        if not event:
            return "【犀牛投资·价格观察】\n" + stamp + "\n视频观察区间发生变化，仅供关注，不构成买卖建议。"
        zone = event.get("zone") or {}
        title = "触达视频观察区间" if event.get("touch_observed") is True else "报价跨过视频观察区间，未确认实际触达"
        return (f"【犀牛投资·价格观察】\n{stamp}\n{_symbol(event.get('symbol'))}：{title}。\n"
                f"报价 {_number(event.get('price'), money=True)}；观察区间 {_number(zone.get('low'), money=True)}–{_number(zone.get('high'), money=True)}。\n"
                f"行情时间：{beijing_time(event.get('quote_time'))}\n视频观点仅供关注，不构成买卖建议。")
    account = report.get("account", "real")
    label = "模拟账户" if account == "paper" else "真实账户"
    kind = report.get("kind") if report.get("kind") in {"每日复盘", "每周复盘", "季度复核", "重大变化"} else "投资更新"
    snapshot = report.get("portfolio") or {}
    lines = [f"【{kind}·{label}】{stamp}", "现金：" + _number(snapshot.get("cash"), money=True)]
    if snapshot.get("broker_snapshot_id"):
        lines[-1] += "（IBKR截图）"
    positions = snapshot.get("positions") or []
    known_symbols = {p.get("symbol") for p in positions}
    holdings = []
    for position in positions[:4]:
        symbol = _symbol(position.get("symbol"))
        holdings.append(f"{symbol} {_number(position.get('quantity'))}股，市值{_number(position.get('market_value'), money=True)}")
    notes = [record for record in snapshot.get("reported_holdings", [])
             if record.get("symbol") not in known_symbols][:max(0, 4 - len(holdings))]
    if notes:
        def local_date(value):
            try:
                return parse_time(value).astimezone(timezone(timedelta(hours=8))).date()
            except (TypeError, ValueError):
                return None
        dates = [local_date(record.get("reported_at")) for record in notes]
        report_date = local_date(report.get("timestamp"))
        same_date = len(set(dates)) == 1
        cutoff = "（截至" + dates[0].strftime("%m月%d日") + "）" if same_date and dates[0] and report_date and dates[0] < report_date else ""
        values = []
        for record, date in zip(notes, dates):
            suffix = ("（" + date.strftime("%m月%d日") + "）") if not same_date and date else "（日期待核对）" if date is None else ""
            values.append(f"{_symbol(record.get('symbol'))} {_number(record.get('reported_market_value'), money=True)}{suffix}")
        reported = "持仓市值（你提供，待核对）" + cutoff + "：" + "；".join(values)
        lines.append(("持仓：" + "；".join(holdings) + "；" if holdings else "") + reported)
    else:
        lines.append("持仓：" + ("；".join(holdings) if holdings else "已核对为空仓" if snapshot.get("holdings_complete") else "尚未核对"))
    advice = [r for r in recommendations if actionable_notice(r, current_plans, now=now or report.get("timestamp")) and r.get("account", account) == account]
    if advice:
        for rec in advice[:1]:
            symbol = _symbol(rec.get("symbol"))
            lines.append(f"动作：{symbol} {ACTIONS.get(rec.get('action'), '等待复核')}，数量{_number(rec.get('quantity'))}股" +
                         (f"，本次预算{_number(rec.get('amount_usd'), money=True)}" if rec.get("action") in {"buy", "add"} else ""))
            lines.append("原因：" + _notification_reason(rec.get("reasons")))
            plan = next(p for p in current_plans if p.get("id") == rec.get("plan_id"))
            entry, stop = plan.get("entry") or {}, plan.get("stop") or {}
            horizon = plan.get("horizon_months")
            horizon_text = str(horizon) + "个月" if isinstance(horizon, int) and not isinstance(horizon, bool) and 12 <= horizon <= 36 else "期限待复核"
            targets = [t for t in plan.get("targets", []) if isinstance(t, dict)]
            target_text = " / ".join(_number(t.get("price"), money=True) for t in targets[:3]) or "待复核"
            lines.append(f"买入区间 {_number(entry.get('low'), money=True)}–{_number(entry.get('high'), money=True)}；目标情景 {target_text}。")
            steps = []
            for step in plan.get("take_profit", [])[:3]:
                if not isinstance(step, dict):
                    continue
                try:
                    percent = f"{decimal_value(step.get('fraction')) * 100:.2f}".rstrip("0").rstrip(".") + "%"
                except (TypeError, ValueError):
                    percent = "待核对"
                steps.append(_number(step.get("price"), money=True) + "卖出" + percent)
            lines.append("分批止盈：" + "；".join(steps or ["规则待复核"]))
            mode = {"daily_close": "常规交易日收盘确认", "weekly_close": "本周最后交易日收盘确认", "intraday": "常规时段盘中确认"}.get(stop.get("confirmation"), "确认方式待核对")
            lines.append(f"止损 {_number(stop.get('price'), money=True)}，{mode}。")
            days = rec.get("holding_days")
            days_text = str(days) + "天" if isinstance(days, int) and not isinstance(days, bool) and days >= 0 else "天数待补"
            lines.append(f"计划持有{horizon_text}；现持{days_text}；建议有效至{beijing_time(rec.get('expires_at'))}。")
    else:
        conditional = sum((p.get("research_assessment") or {}).get("status") == "approved_conditional"
                          and p.get("status") not in {"invalidated", "superseded"} for p in current_plans)
        pending = sum(p.get("status") != "active" and (p.get("research_assessment") or {}).get("status") != "approved_conditional"
                      for p in current_plans)
        risky_symbols = {_symbol(r.get("symbol")) for r in recommendations
                         if {reason for reason in r.get("reasons", []) if isinstance(reason, str)} &
                         {"confirmed_price_stop", "verified_thesis_invalidation"}}
        exposed = {p.get("symbol") for p in positions if decimal_value(p.get("quantity") or 0) > 0}
        exposed.update(p.get("symbol") for p in snapshot.get("reported_holdings", [])
                       if decimal_value(p.get("reported_market_value") or 0) > 0)
        if risky_symbols & exposed:
            lines.append("结论：持仓风险需人工核查，当前不是下单指令。")
        else:
            details = ([f"{conditional}项估值已审核，入场条件待满足"] if conditional else [])
            if pending:
                details.append(f"{pending}项研究待核实")
            lines.append("结论：暂无新增操作建议" + ("；" + "；".join(details) if details else "") + "。")
    gaps = []
    if snapshot.get("cash") is None:
        gaps.append("实际现金")
    if not snapshot.get("holdings_complete"):
        gaps.append("持仓股数、成本及买入时间，并完成持仓核对")
    elif not snapshot.get("cost_complete") or not snapshot.get("fees_complete"):
        gaps.append("持仓成本及实际费用")
    if advice:
        extra = f"另有{len(advice) - 1}项操作，见工作台。" if len(advice) > 1 else ""
        if gaps or extra:
            lines.append(("待补：" + "；".join(gaps) + "。" if gaps else "") + extra)
    else:
        lines.append("待补：" + "；".join(gaps) + "。" if gaps else "资料：账户信息已核对。")
    safe_url = table_url if isinstance(table_url, str) and re.fullmatch(r"https://[A-Za-z0-9.\-]+/(?:base|wiki)/[A-Za-z0-9]+", table_url) else None
    lines.append("工作台：" + safe_url if safe_url else "详情：打开投资工作台。")
    return "\n".join(lines)


def _rows(value):
    return value if isinstance(value, list) else []


def _take_profit(value):
    rows = []
    for step in _rows(value):
        if not isinstance(step, dict):
            rows.append("未知分批规则；需复核")
            continue
        try:
            fraction = str(decimal_value(step.get("fraction")) * 100) + "%"
        except (ValueError, TypeError):
            fraction = "未知比例"
        rows.append(f"${step.get('price', '未知')}：卖出{fraction}")
    return "\n".join(rows) or "未提供；需复核"


def record_reported_holding(store, symbol, reported_market_value, *, reported_at, account="real"):
    """Preserve a user's incomplete holding statement; never create a ledger entry."""
    if account != "real":
        raise ValueError("reported holdings must belong to the real account")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) or symbol.endswith(".US"):
        raise ValueError("symbol must be a bare uppercase US symbol")
    value = decimal_value(reported_market_value, "reported_market_value")
    if value < 0:
        raise ValueError("reported_market_value cannot be negative")
    stamp = parse_time(reported_at).isoformat()
    identity = stable_id("reported_holding", account, symbol, stamp)
    record = {"id": identity, "account": account, "symbol": symbol,
              "reported_market_value": str(value), "reported_at": stamp,
              "source": "user_statement", "currency": "USD", "status": "unreconciled",
              "quantity": None, "cost_basis": None, "first_fill_at": None,
              "market_price": None, "unrealized_pnl": None}
    store.put("reported_holding", identity, record, immutable=True)
    return record


def reported_holdings(store, account="real", *, now=None):
    """Latest statement per symbol, retaining every immutable historical version."""
    stamp = parse_time(now or utc_now())
    latest = {}
    for _, record in store.items("reported_holding"):
        if record.get("account") != account or parse_time(record["reported_at"]) > stamp:
            continue
        symbol = record["symbol"]
        if symbol not in latest or parse_time(record["reported_at"]) > parse_time(latest[symbol]["reported_at"]):
            latest[symbol] = record
    from .broker_snapshot import latest_broker_snapshot
    broker = latest_broker_snapshot(store, account, now=now)
    if broker:
        for symbol in broker.get("supersedes_symbols", []):
            if symbol in latest and parse_time(latest[symbol]["reported_at"]) <= parse_time(broker["recorded_at"]):
                del latest[symbol]
        for holding in broker.get("holdings", []):
            symbol = holding["symbol"]
            if symbol in latest and parse_time(latest[symbol]["reported_at"]) > parse_time(broker["recorded_at"]):
                continue
            latest[symbol] = dict(holding, account=account, source="broker_screenshot",
                reported_market_value=holding["market_value"], reported_at=broker["recorded_at"],
                market_price=holding.get("last_price"), broker=broker["broker"], broker_as_of=broker.get("as_of"))
    return [latest[symbol] for symbol in sorted(latest)]


def with_reported_holdings(store, snapshot):
    """Preserve reported brokerage balances separately from ledger-derived totals."""
    from .broker_snapshot import with_broker_snapshot
    snapshot = with_broker_snapshot(store, snapshot)
    recorded = {position.get("symbol") for position in snapshot.get("positions", [])}
    return dict(snapshot, reported_holdings=[record for record in reported_holdings(
        store, snapshot.get("account", "real"), now=snapshot.get("as_of")) if record["symbol"] not in recorded])


def portfolio_rows(snapshot):
    rows = []
    for position in snapshot.get("positions", []):
        symbol = position.get("symbol", "未知")
        weight, days = None, None
        try:
            equity = decimal_value(snapshot.get("total_equity"))
            weight = str((decimal_value(position.get("market_value")) / equity * 100).quantize(decimal_value("0.01"))) + "%" if equity > 0 else None
        except (ValueError, TypeError):
            pass
        try:
            days = max(0, (parse_time(snapshot.get("as_of") or utc_now()) - parse_time(position.get("first_fill_at"))).days)
        except (ValueError, TypeError):
            pass
        rows.append({"系统标识": f"{snapshot.get('account', 'real')}:{symbol}",
                     "账户": snapshot.get("account", "real"), "股票": symbol,
                     "数量": readable(position.get("quantity")),
                     "成本": readable(position.get("cost_basis_net", position.get("cost_basis", position.get("cost")))),
                     "市价": readable(position.get("price", position.get("market_price"))),
                     "市值": readable(position.get("market_value")),
                     "未实现盈亏": readable(position.get("unrealized_pnl_net", position.get("unrealized_pnl"))),
                     "持有天数": readable(days), "占总资产比例": readable(weight),
                     "数据状态": readable(position.get("valuation_status", snapshot.get("valuation_status"))),
                     "更新时间": snapshot.get("as_of", utc_now())})
    recorded = {position.get("symbol") for position in snapshot.get("positions", [])}
    for record in snapshot.get("reported_holdings", []):
        if record.get("account") != snapshot.get("account", "real") or record["symbol"] in recorded:
            continue
        rows.append({"系统标识": f"{record['account']}:{record['symbol']}",
                     "账户": record["account"], "股票": record["symbol"],
                     "数量": readable(record.get("quantity")), "成本": readable(record.get("cost_basis")), "市价": readable(record.get("market_price")),
                     "市值": f"用户报告 {record['reported_market_value']} USD（待核对；非系统估值）",
                     "未实现盈亏": readable(None), "持有天数": readable(None), "占总资产比例": readable(None),
                     "数据状态": ("IBKR截图已录入；股数、成本、买入日期未显示；截图行情时间未显示；记录时间：" if record.get("source") == "broker_screenshot" else
                         "用户报告持仓待核对；股数、成本及首次建仓时间未知；未计入账本及资产合计；用户报告时间：") + record["reported_at"],
                     "更新时间": record["reported_at"]})
    return rows


def broker_account_fields(snapshot):
    if not snapshot.get("broker_snapshot_id"):
        return {}
    fields = {"券商": "IBKR", "券商账户": readable(snapshot.get("broker_account_identifier")),
        "现金来源": "IBKR截图；余额已记录，尚未与逐笔成交核对", "净值来源": "IBKR截图账户净值，非系统逐笔估值",
        "截图记录时间": snapshot["broker_snapshot_recorded_at"], "截图行情时间": snapshot.get("broker_snapshot_as_of") or "截图未显示",
        "现金完整": "截图余额已记录；逐笔账目尚未核对", "持仓核对状态": "已记录IBKR持仓；股数、成本和买入日期未显示",
        "成本完整": "截图未显示", "费用完整": "截图未显示",
        "持仓市值美元": readable(snapshot.get("broker_reported_holdings_market_value_sum")) + "（持仓截图合计）",
        "可用现金美元": "执行前核对券商可用现金；购买力不计入现金"}
    for label, key in {"券商未实现盈亏美元": "unrealized_pnl", "券商已实现盈亏美元": "realized_pnl",
                       "维持保证金美元": "maintenance_margin", "剩余流动性美元": "excess_liquidity",
                       "购买力美元": "buying_power", "股息美元": "dividends",
                       "对账差额美元": "unexplained_non_cash_value"}.items():
        fields[label] = readable(snapshot.get("broker_reported_" + key))
    if snapshot.get("cash_status") == "new_ledger_cash_event_requires_broker_refresh":
        fields.update(现金来源="截图后已录入新的现金变动，历史余额见券商快照", 现金完整="需要更新券商余额")
    return fields


def plan_row(plan, recommendation=None):
    rec = recommendation or {}
    entry, stop = plan.get("entry"), plan.get("stop")
    entry = entry if isinstance(entry, dict) else {}
    stop = stop if isinstance(stop, dict) else {}
    risk = rec.get("risk_context") or {}
    conditional = ((plan.get("research_assessment") or {}).get("status") == "approved_conditional"
                   and plan.get("status") not in {"invalidated", "superseded"})
    row = {"系统标识": plan["id"], "股票": plan.get("symbol", ""),
            "版本": readable(plan.get("revision")), "状态": {"active": "已审核", "candidate": "待审核", "needs_review": "待审核", "invalidated": "已失效", "superseded": "已替代"}.get(plan.get("status"), "待审核"),
            "建议动作": ACTIONS.get(rec.get("action"), "等待审核"), "理由": human_reasons(rec.get("reasons", [])),
            "推荐标识": rec.get("id", ""), "入场批次": readable(rec.get("tranche_id", rec.get("tranche"))),
            "投资逻辑": readable(plan.get("thesis")), "期限月数": readable(plan.get("horizon_months")),
            "买入区间": f"{readable(entry.get('low'))}–{readable(entry.get('high'))} USD",
            "单次买入美元": readable(rec.get("amount_usd") if rec.get("amount_usd") is not None else entry.get("amount_usd")),
            "目标情景": "\n".join(f"{t.get('scenario', '')}: ${t.get('price', '未知')} / {t.get('horizon_months', '?')}个月; {t.get('method', '')}" for t in _rows(plan.get("targets")) if isinstance(t, dict)),
            "分批止盈": _take_profit(plan.get("take_profit")), "止损价": readable(stop.get("price")),
            "止损确认方式": {"daily_close": "常规交易日收盘确认", "weekly_close": "本周最后交易日收盘确认", "intraday": "常规时段盘中确认"}.get(stop.get("confirmation"), readable(stop.get("confirmation"))),
            "逻辑失效条件": "\n".join(readable(c) for c in _rows(plan.get("invalidation_conditions"))),
            "到期处理": readable(plan.get("time_exit_policy")),
            "下次复核": readable(plan.get("next_review_at")),
            "持有天数": readable(rec.get("holding_days")), "建议有效期": readable(rec.get("expires_at")),
            "每股止损风险美元": readable(risk.get("price_risk_per_share_usd")),
            "价格止损风险百分比": readable(risk.get("price_risk_percent")),
            "本次投入止损风险美元": readable(risk.get("proposed_price_risk_usd")),
            "当前持仓止损风险美元": readable(risk.get("holding_price_risk_usd")),
            "情景年化收益估算": "\n".join(f"{s.get('scenario', '情景')}：累计{s.get('return_percent', '未知')}%；年化{s.get('annualized_return_percent', '未知')}%（模型假设）" for s in rec.get("scenario_returns", [])),
            "证据引用": readable(plan.get("evidence_refs", [])),
            "风险状态": ("最大回撤硬限制未配置；需人工判断" if risk.get("drawdown_limit") is None else
                         f"已设置回撤上限{risk['drawdown_limit']}；评估状态：{risk.get('drawdown_status', '未知')}；缺少历史净值时不能确认通过"),
            "更新时间": utc_now()}
    if plan.get("status") != "active" or conditional:
        for key in ("买入区间", "目标情景", "分批止盈", "止损价", "止损确认方式", "情景年化收益估算",
                    "每股止损风险美元", "价格止损风险百分比", "本次投入止损风险美元", "当前持仓止损风险美元",
                    "投资逻辑", "逻辑失效条件", "到期处理", "单次买入美元", "期限月数", "下次复核"):
            row[key] = "待审核，暂不展示"
        row["建议动作"] = "等待审核"
        labels = {"EPS_UNIT_UNVERIFIED": "每股盈利口径尚待核实", "INSTRUMENT_UNIT_UNVERIFIED": "证券报价单位尚待核实",
                  "ETF_BASELINE_VALUE_MISSING": "基金净值或估值基准待补齐", "ETF_BASELINE_DATES_UNALIGNED": "基金净值与估值日期未对齐",
                  "ETF_HOLDINGS_COVERAGE_MISSING": "基金成分持仓覆盖不足", "ETF_FEES_UNRESOLVED": "基金费用口径待核实",
                  "ETF_ISSUER_EVIDENCE_UNVERIFIED": "基金官方资料待核实", "RESEARCH_REVIEW_REJECTED": "独立复核仍有实质疑点"}
        gaps = list(dict.fromkeys(labels[g["code"]] for g in plan.get("research_gaps", [])
            if isinstance(g, dict) and g.get("blocking") and g.get("code") in labels and g.get("scope") != "execution"))
        row["理由"] = "；".join(gaps[:3]) if gaps else "研究资料尚未通过独立审核，暂无新增操作建议"
        row["证据引用"] = "来源记录已内部留存，待独立审核"
    if (plan.get('research_assessment') or {}).get('status')=='approved_no_buy' and plan.get('status') not in {'invalidated','superseded'}:
        row.update(状态='研究已通过·暂不买',建议动作='不新增买入',单次买入美元='0',买入区间='无买入建议',
                   理由='独立审核后的估值不支持新增买入；本次新增投入0美元。',证据引用='估值证据与独立审核记录已内部留存')
    elif conditional:
        row["状态"] = "估值已审核·入场条件待满足"
        row["建议动作"] = "等待入场条件"
        row["理由"] = "估值已通过独立审核；仍需核实入场条件并形成完整入场规则，当前暂无操作建议"
        row["证据引用"] = "估值证据与独立审核记录已内部留存；入场条件待满足"
    elif plan.get('status') == 'needs_review' and (plan.get('research_assessment') or {}).get('status') == 'needs_evidence':
        from .research_attention import attention_items
        item = attention_items([plan])[0]
        row['状态'] = item['status']
        row['建议动作'] = '等待补证据后复核'
        row['理由'] = '；'.join(item['reasons']) + '；证据更新后继续自动复核'
    return row


def make_report(kind, period, portfolio, plans=(), recommendations=(), *, now=None):
    now = now or utc_now()
    account = portfolio.get("account", "real")
    cash = readable(portfolio.get("cash"))
    lines = [f"{kind} · {period} · {account}",
             f"快照生成时间：{now}；周期用于调度归属，非补造历史收盘持仓。",
             f"目标总资产（持仓＋现金）：20,000 USD；实际现金：{cash} USD",
             f"已录入持仓市值：{readable(portfolio.get('market_value'))} USD；总权益：{readable(portfolio.get('total_equity'))} USD",
             "持仓核对：" + ("已核对" if portfolio.get('holdings_complete') else "未核对，资产可能尚未录全"),
             f"现金完整：{readable(portfolio.get('cash_complete'))}；成本完整：{readable(portfolio.get('cost_complete'))}；费用完整：{readable(portfolio.get('fees_complete'))}",
             ("最大回撤硬限制：未配置。" if portfolio.get("drawdown_limit") is None else
              f"最大回撤硬限制：{portfolio['drawdown_limit']}；历史净值峰值不足，不能评估。") + "真实成交须由用户执行并确认；模拟账户单列。",
             f"计划数：{len(plans)}；本次评估：{len(recommendations)}"]
    for rec in recommendations:
        lines.append(f"{rec.get('symbol', '?')}：{ACTIONS.get(rec.get('action'), '等待复核')}；{human_reasons(rec.get('reasons', []))}")
        lines.append(f"本次建议金额${readable(rec.get('amount_usd'))}；数量{readable(rec.get('quantity'))}股；投资期限{readable(rec.get('horizon_months'))}个月")
        risk = rec.get('risk_context') or {}
        lines.append(f"持有{readable(rec.get('holding_days'))}天；建议有效至{readable(rec.get('expires_at'))}；每股价格止损风险${readable(risk.get('price_risk_per_share_usd'))}（{readable(risk.get('price_risk_percent'))}%）；当前持仓止损风险${readable(risk.get('holding_price_risk_usd'))}")
        for scenario in _rows(rec.get('scenario_returns')):
            if isinstance(scenario, dict):
                lines.append(f"{scenario.get('scenario', '估值情景')}：累计收益估算{readable(scenario.get('return_percent'))}%；年化估算{readable(scenario.get('annualized_return_percent'))}%（情景假设，非收益承诺）")
    for plan in plans:
        row = plan_row(plan)
        lines.extend([f"{row['股票']} 投资计划（{row['状态']}）",
            f"买入区间：{row['买入区间']}；规划单次投入：${row['单次买入美元']}；计划持有：{row['期限月数']}个月",
            f"目标情景（非收益承诺）：{row['目标情景'] or '未通过估值复核'}",
            f"分批止盈：{row['分批止盈']}；止损价：${row['止损价']}；确认方式：{row['止损确认方式']}",
            f"逻辑失效条件：{row['逻辑失效条件'] or '尚待复核'}；到期处理：{row['到期处理']}",
            f"下次复核：{row['下次复核']}；依据：{row['证据引用']}"])
    for row in portfolio_rows(portfolio):
        lines.append(f"{row['股票']}：{row['数量']}股，市值：{row['市值']}，占总资产{row['占总资产比例']}，持有{row['持有天数']}天，未实现盈亏${row['未实现盈亏']}；数据状态：{row['数据状态']}；记录时间：{row['更新时间']}")
    body = "\n".join(lines)
    report = {"id": stable_id("report", kind, period, account), "kind": kind, "period": period,
            "account": account, "timestamp": now, "body": body,
            "portfolio": portfolio, "recommendation_ids": [r.get("id") for r in recommendations]}
    report["notification_body"] = notification_text(report, current_plans=plans, recommendations=recommendations)
    return report


def report_row(report):
    p = report.get("portfolio", {})
    if report.get("kind") == "价格观察":
        return {"系统标识": report["id"], "报告类型": report["kind"], "报告周期": report["period"],
                "账户": "视频观察", "现金美元": "不适用", "持仓市值美元": "不适用",
                "总权益美元": "不适用", "回撤限制": "不适用", "正文": report.get("notification_body") or notification_text(report), "生成时间": report["timestamp"]}
    return {"系统标识": report["id"], "报告类型": report["kind"], "报告周期": report["period"],
            "账户": report.get("account", "real"), "现金美元": readable(p.get("cash")),
            "持仓市值美元": readable(p.get("market_value")), "总权益美元": readable(p.get("total_equity")),
            "回撤限制": "未配置" if p.get("drawdown_limit") is None else str(p["drawdown_limit"]) + "；不能评估", "正文": report.get("notification_body") or notification_text(report), "生成时间": report["timestamp"]}
