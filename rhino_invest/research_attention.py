"""Notify research outcomes separately from executable advice, with durable deduplication."""
from .common import stable_id
from .feishu import UnknownOutcome


def attention_items(plans, store=None):
    items = []
    for plan in plans:
        if plan.get('status') in {'invalidated', 'superseded'}:
            continue
        assessment = plan.get('research_assessment') or {}
        ready = plan.get('status') == 'active' and assessment.get('status') == 'approved'
        if plan.get('status') == 'active' and not ready:
            continue
        conditional = assessment.get('status') == 'approved_conditional'
        no_buy = assessment.get('status') == 'approved_no_buy'
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
        if no_buy:
            status='研究已通过·暂不买'
            reasons=['现有证据不足以支持新增买入，本次新增投入0美元' if (plan.get('valuation_basis') or {}).get('kind')=='insufficient_valuation'
                     else '估值不支持新增买入，本次新增投入0美元']
        elif ready:
            status = '研究已通过·按条件观察'
            reasons = ['只有价位、账户资金和执行检查均满足时才考虑买入']
        elif conditional:
            status = '估值已审核·入场条件待满足'
            reasons = ['入场条件尚未核实为可执行规则']
        elif reviewed:
            status = '已自动审核·未通过'
        else:
            status = '资料校验未通过·待补证据' if preflight else '待补证据或自动审核'
        if not reasons:
            reasons = ['独立审核仍有证据缺口'] if reviewed else ['尚无完整审核结果']
        item = {'symbol': plan.get('symbol', '?'), 'status': status,
                'reasons': sorted(set(reasons)), 'conditional': conditional}
        if no_buy:item['no_buy']=True
        if ready:
            item['research_ready'] = True
        if store is not None and (ready or conditional):
            from .research import plan_fingerprint
            binding = store.get('research_plan_review',plan.get('review_evidence_id','')) or {}
            entry = plan.get('entry') or {}
            if binding.get('accepted') is True and binding.get('plan_hash') == plan_fingerprint(plan):
                if entry.get('low') is not None and entry.get('high') is not None:
                    item['buy_observation'] = {key:entry.get(key) for key in ('low','high','amount_usd')}
                    item['buy_observation']['source'] = {
                        'independent_valuation': '系统估算', 'source_level': '视频中的价位'
                    }.get(entry.get('basis'), '来源尚未标明')
                    if plan.get('price_basis_valid_until'):
                        item['buy_observation']['basis_valid_until'] = plan['price_basis_valid_until']
        items.append(item)
    return sorted(items, key=lambda item: (item['symbol'], item['status']))


def attention_text(items, when):
    lines = ['【犀牛投资·研究审核结果】' + when]
    if not items:
        lines += ['此前提示的问题已解除。', '这不代表现在可以买入；买入建议会另行说明股票、价格、金额和条件。']
    else:
        ready = [item for item in items if item.get('research_ready')]
        conditional = [item for item in items if item['conditional']]
        rejected = [item for item in items if not item['conditional'] and not item.get('research_ready') and not item.get('no_buy')]
        no_buy = [item for item in items if item.get('no_buy')]
        lines.append('结论：本次更新没有可直接执行的买入建议。' if not ready
                     else '结论：有研究通过，但买入前仍需检查最新报价和账户资金。')
        lines.append('你无需补财报；这条通知不要求你下单。')
        if no_buy:
            lines += ['', '已完成研究，暂不买：']
            lines += [item['symbol']+'｜'+item['reasons'][0]+'。' for item in no_buy]
        if ready or conditional:
            lines += ['', '可以继续关注：']
        for item in ready + conditional:
            lines.append(item['symbol']+'｜'+('研究已通过，等买入前检查。' if item.get('research_ready')
                                             else '暂不买：买入条件还没全部核实。'))
            observation = item.get('buy_observation')
            if observation:
                lines.append(f"  观察价 {observation['low']}–{observation['high']} 美元／股（{observation['source']}）。")
                if observation.get('amount_usd'):
                    lines.append(f"  条件满足后首笔预算 {observation['amount_usd']} 美元；目前不据此下单。")
                lines.append('  等待多久无法预测，也可能一直不到价；到价后仍需复核。')
                if observation.get('basis_valid_until'):
                    from datetime import timezone,timedelta
                    from .common import parse_time
                    deadline=parse_time(observation['basis_valid_until']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
                    lines.append('  旧价位在'+deadline+'（北京时间）失效，届时须重新计算。')
        if rejected:
            lines += ['', '这些计划暂时不能用：']
            explanations = {
                '止盈或止损规则缺失、存在冲突': '买入后何时卖出的规则没写完整或相互矛盾',
                '估值情景或计算依据未通过核验': '估价计算或计算依据还没核对通过',
                '每股盈利口径或估算假设待核实': '估算未来利润的依据还不够可靠',
                '增长假设或预测期限待核实': '增长速度或预测年份还没核对清楚',
                '入场规则或价格单位待核实': '买入规则或拆股前后价格还没核对清楚',
            }
            for item in rejected:
                reasons=[explanations[r] for r in item['reasons'] if r in explanations]
                lines.append(item['symbol']+'｜'+('；'.join(reasons[:2]) if reasons else '资料或分析还没核实完成')+'。')
        lines += ['', '后续：公开资料和计划错误由系统处理，你不用替系统补材料。',
                  '有买入建议或确需账户信息时，会单独写清。详细记录见飞书“投资计划”。']
    return '\n'.join(lines)


def notify_attention(store, feishu, plans, *, now, when, enabled, execute):
    """Persist before delivery; never turn an unknown send into a fresh identity."""
    items = attention_items(plans,store)
    signature = stable_id('research-attention-v3-plain-language', items)
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
