"""Notify research outcomes separately from executable advice, with durable deduplication."""
from .common import stable_id
from .feishu import UnknownOutcome


def attention_items(plans):
    items = []
    for plan in plans:
        if plan.get('status') in {'active', 'invalidated', 'superseded'}:
            continue
        assessment = plan.get('research_assessment') or {}
        conditional = assessment.get('status') == 'approved_conditional'
        preflight = bool(plan.get('research_preflight'))
        reviewed = not preflight and (bool(plan.get('review_evidence_id')) or assessment.get('status') == 'needs_evidence')
        details = ' '.join(str(v) for v in plan.get('review_issues', []))
        gaps = [g for g in plan.get('research_gaps', []) if isinstance(g, dict)]
        details += ' ' + ' '.join(str(g.get('code', ''))+' '+str(g.get('detail', '')) for g in gaps)
        lower = details.lower()
        reasons = []
        for tokens, label in [
            (('take_profit', 'take-profit', 'stop.price', 'stop at'), '止盈或止损规则缺失、存在冲突'),
            (('base/downside', 'target scenarios', 'scenario arithmetic', 'bridge_arithmetic', 'arithmetic_inconsistent'), '估值情景或计算依据未通过核验'),
            (('eps', 'per-share', 'per_share', 'dilution'), '每股盈利口径或估算假设待核实'),
            (('growth_', 'growth rate', 'horizon', 'forward_period'), '增长假设或预测期限待核实'),
            (('entry_condition', 'executable price predicate', 'price unit', 'split'), '入场规则或价格单位待核实')]:
            if any(token in lower for token in tokens):
                reasons.append(label)
        if conditional:
            status = '估值已审核·入场条件待满足'
            reasons = ['入场条件尚未核实为可执行规则']
        elif reviewed:
            status = '已自动审核·未通过'
        else:
            status = '资料校验未通过·待补证据' if preflight else '待补证据或自动审核'
        if not reasons:
            reasons = ['独立审核仍有证据缺口'] if reviewed else ['尚无完整审核结果']
        items.append({'symbol': plan.get('symbol', '?'), 'status': status,
                      'reasons': sorted(set(reasons)), 'conditional': conditional})
    return sorted(items, key=lambda item: (item['symbol'], item['status']))


def attention_text(items, when):
    lines = ['【犀牛投资·研究审核结果】' + when]
    if not items:
        lines.append('此前提示的研究审核阻塞已解除。是否可操作仍以当前执行条件为准。')
    else:
        rejected = sum(not item['conditional'] for item in items)
        conditional = len(items) - rejected
        lines.append(f'需处理：{rejected} 份未通过或待补证据；{conditional} 份估值已审核、入场条件待满足。')
        for item in items:
            lines.append(item['symbol']+'｜'+item['status']+'：'+'；'.join(item['reasons']))
        lines.append('后续：证据更新后继续自动复核；入场条件未核实前不生成操作建议。详细意见见飞书“投资计划”。')
        lines.append('同一股票、问题及审核状态不变时不重复提醒；问题变化或解除时再通知。')
    lines.append('非投资建议。')
    return '\n'.join(lines)


def notify_attention(store, feishu, plans, *, now, when, enabled, execute):
    """Persist before delivery; never turn an unknown send into a fresh identity."""
    items = attention_items(plans)
    signature = stable_id('research-attention-v1', items)
    previous = store.get('research_attention_state', 'current')
    if not enabled or not execute:
        return {'status': 'disabled' if not enabled else 'dry_run', 'items': items}
    if previous and previous.get('status') in {'sending', 'unknown'}:
        operations = [v for _, v in store.items('feishu_outbox')
                      if v.get('identity') == 'notice:'+previous['id']]
        confirmed = next((v for v in operations if v.get('status') in {'delivered', 'acknowledged'}), None)
        if confirmed:
            previous = dict(previous, status='sent', receipt=confirmed.get('result', {}))
            store.put('research_attention_state', 'current', previous)
            store.put('research_attention_notice', previous['id'], previous)
        else:
            raise UnknownOutcome('Research outcome notice is unresolved; preserve the original send identity')
    if previous and previous.get('signature') == signature and previous.get('status') == 'sent':
        return {'status': 'unchanged', 'items': items}
    if not previous and not items:
        return {'status': 'idle', 'items': []}
    sequence = (previous or {}).get('sequence', 0) + int(not previous or previous.get('signature') != signature)
    identity = stable_id('research-attention-notice-v1', sequence, signature)
    record = {'id': identity, 'sequence': sequence, 'signature': signature, 'status': 'sending',
              'items': items, 'timestamp': now}
    store.put('research_attention_state', 'current', record)
    store.put('research_attention_notice', identity, record)
    try:
        receipt = feishu.notify(identity, attention_text(items, when))
    except Exception:
        record = dict(record, status='unknown')
        store.put('research_attention_state', 'current', record)
        store.put('research_attention_notice', identity, record)
        raise
    record = dict(record, status='disabled' if receipt.get('status') == 'disabled' else 'sent', receipt=receipt)
    store.put('research_attention_state', 'current', record)
    store.put('research_attention_notice', identity, record)
    return {'status': record['status'], 'items': items}
