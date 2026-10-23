"""Readable, evidence-aware Feishu projections, separate from accounting truth."""
from __future__ import annotations

import json
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
    return rows


def plan_row(plan, recommendation=None):
    rec = recommendation or {}
    entry, stop = plan.get("entry"), plan.get("stop")
    entry = entry if isinstance(entry, dict) else {}
    stop = stop if isinstance(stop, dict) else {}
    risk = rec.get("risk_context") or {}
    return {"系统标识": plan["id"], "股票": plan.get("symbol", ""),
            "版本": readable(plan.get("revision")), "状态": plan.get("status", "needs_review"),
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
        lines.append(f"{row['股票']}：{row['数量']}股，市值${row['市值']}，占总资产{row['占总资产比例']}，持有{row['持有天数']}天，未实现盈亏${row['未实现盈亏']}")
    body = "\n".join(lines)
    return {"id": stable_id("report", kind, period, account), "kind": kind, "period": period,
            "account": account, "timestamp": now, "body": body,
            "portfolio": portfolio, "recommendation_ids": [r.get("id") for r in recommendations]}


def report_row(report):
    p = report.get("portfolio", {})
    if report.get("kind") == "价格观察":
        return {"系统标识": report["id"], "报告类型": report["kind"], "报告周期": report["period"],
                "账户": "视频观察", "现金美元": "不适用", "持仓市值美元": "不适用",
                "总权益美元": "不适用", "回撤限制": "不适用", "正文": report["body"], "生成时间": report["timestamp"]}
    return {"系统标识": report["id"], "报告类型": report["kind"], "报告周期": report["period"],
            "账户": report.get("account", "real"), "现金美元": readable(p.get("cash")),
            "持仓市值美元": readable(p.get("market_value")), "总权益美元": readable(p.get("total_equity")),
            "回撤限制": "未配置" if p.get("drawdown_limit") is None else str(p["drawdown_limit"]) + "；不能评估", "正文": report["body"], "生成时间": report["timestamp"]}
