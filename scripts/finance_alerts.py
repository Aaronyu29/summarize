"""Durable, deduplicated Lark failure notices; never include raw exceptions."""
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
import sqlite3

from finance_feishu import signature
from finance_recovery import ReviewRejected


def reason(error):
    if isinstance(error, ReviewRejected):
        issues = [' '.join(re.sub(r'https?://\S+', '[来源已归档]', str(item)).split())[:160]
                  for item in error.issues[:3]]
        return '自动修订后证据审核仍未通过：' + '；'.join(issues)
    text = str(error)
    rules = [('Join this channel to get access', '当前账号无权读取会员专享视频'),
             ('members-only content', '当前账号无权读取会员专享视频'),
             ("Sign in to confirm", 'YouTube 要求重新验证登录状态，需更新授权凭据'),
             ('Unknown video availability', '尚未确认视频是否公开可读取'),
             ('Second-pass review', '二次审核未通过，已停止发布，需检查证据与结论'),
             ('Incomplete model output', '模型回复达到输出上限，完整审核尚未完成，已保留回执'),
             ('Evidence references must', '证据编号格式不符合要求，已停止发布'),
             ('Unknown transcript evidence ID', '报告引用了不存在的转写段落，已停止发布'),
             ('Timestamp is not aligned', '结论时间与引用发言不对齐，已停止发布'),
             ('Frame missing or not aligned', '所选画面缺失或与发言时间不对齐，已停止发布'),
             ('Overview exceeds', '摘要超过长度上限，需精简后重新核验'),
             ('balanced emphasis', '正文重点标记不完整，需修正后重新核验'),
             ('Analysis does not meet coverage', '报告重点数或配图覆盖不足，需补齐后重新核验'),
             ('Video model-call ceiling', '本期自动分析请求数已达到上限，已保留全部材料并停止新增付费请求'),
             ('replacement is exhausted', '本期自动替代请求次数已用尽，旧回执仍不明，已停止重复付费请求'),
             ('allowance is exhausted', '本期自动替代请求次数已用尽，已停止重复付费请求'),
             ('older unknown model request', '存在无法自动关联的旧未决请求，已保留回执并停止新增付费请求'),
             ('Video metadata temporarily', '暂时无法取得视频元数据，系统已安排后续检查'),
             ('publication', '无法确认视频的真实发布时间'),
             ('cookies', 'YouTube 登录凭据缺失或权限不符合要求'),
             ('8 GiB', '剩余磁盘不足 8 GiB，已停止新下载以保留档案'),
             ('pending', '远端请求结果待确认，已保留检查点，避免重复写入'),
             ('Unreconciled', '存在结果不明的远端写入，需核实回执'),
             ('outcome unknown', '远端请求结果不明，需核实回执'),
             ('transcript', '转写完整性或证据校验未通过'),
             ('Evidence', '分析证据引用校验未通过'),
             ('Visual', '画面分析遗漏或时间戳校验未通过'),
             ('Document content', '飞书正文读回核验未通过'),
             ('bind', '飞书图片绑定核验未通过'),
             ('Missing configuration', '服务所需配置缺失')]
    for token, description in rules:
        if token.lower() in text.lower():
            return description
    code = re.search(r'(?:HTTP(?: status)?|business code|job_status=)[ =]*(\d{3,10})', text)
    if code:
        return f'远端接口拒绝请求，状态码 {code.group(1)}'
    if isinstance(error, TimeoutError):
        return '网络请求超时'
    if isinstance(error, OSError):
        return '文件、磁盘或网络操作失败，需检查服务器日志'
    return '处理失败，需检查服务器日志（' + type(error).__name__ + '）'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def enqueue(root, source, stage, error, action, *, identity_key=None):
    description = reason(error)
    identity = hashlib.sha256(json.dumps(identity_key or [source.get('video_id'), stage, description, action],
                                        ensure_ascii=False).encode()).hexdigest()[:20]
    path = pathlib.Path(root) / 'alerts' / (identity + '.json')
    if path.exists():
        return path
    published = '未知（未能取得发布日期）'
    try:
        moment = dt.datetime.fromisoformat(source['published_utc'].replace('Z', '+00:00'))
        if moment.tzinfo is not None:
            published = moment.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）'
    except (KeyError, ValueError, TypeError):
        pass
    vid = source.get('video_id', '')
    link = 'https://www.youtube.com/watch?v=' + vid if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid) else ''
    text = (f"【RhinoFinance 自动任务失败】\n视频：{source.get('title', '频道扫描/服务运行')[:300]}"
            f"\n视频发布：{published}\n失败阶段：{stage}\n失败原因：{description}"
            f"\n处理状态：{action}\n告警编号：{identity}\n{link}")
    save(path, {'id': identity, 'text': text, 'status': 'queued', 'attempts': 0})
    return path


def scan_missing_reports(root, *, now=None):
    """Independent alert path; a busy video worker cannot hide a missing report."""
    path=pathlib.Path(root)/'jobs.sqlite'
    if not path.exists():return 0
    moment=time.time() if now is None else now;queued=0
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute('SELECT video_id,source,status,checkpoint,updated FROM jobs').fetchall()
    for vid,source,status,raw,updated in rows:
        cp=json.loads(raw);delivery=cp.get('delivery') or {};draft=cp.get('human_review_delivery') or {}
        if (delivery.get('status') in {'delivered','already_delivered'} or
                draft.get('status') in {'delivered','already_delivered'}):continue
        if status=='needs_review' or (status in {'queued','retry','processing'} and moment-updated>=5400):
            enqueue(root,json.loads(source),'人工审核文档待交付',ReviewRejected(cp.get('review_issues') or ['处理超过90分钟或自动任务已暂停，尚未交付文档。']),
                '目前没有已交付的正式文档或人工审核草稿；请核查原视频及发布状态。',
                identity_key=['human-review-missing',vid]);queued+=1
    return queued


def flush(root, env, session=None):
    if session is None:
        import requests
        session = requests.Session()
    counts = {'sent': 0, 'pending': 0}
    for path in sorted((pathlib.Path(root) / 'alerts').glob('*.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        if item['status'] == 'sent':
            continue
        payload = {'msg_type': 'text', 'content': {'text': item['text']}}
        if env.get('LARK_WEBHOOK_SECRET'):
            payload['timestamp'] = str(int(time.time()))
            payload['sign'] = signature(payload['timestamp'], env['LARK_WEBHOOK_SECRET'])
        item['attempts'] += 1
        item['status'] = 'sending'
        save(path, item)
        try:
            response = session.post(env['LARK_WEBHOOK_URL'], json=payload, timeout=(10, 30), allow_redirects=False)
            body = response.json()
            if response.status_code != 200 or body.get('code', body.get('StatusCode')) != 0:
                raise RuntimeError('Alert rejected')
        except Exception:
            item['status'] = 'retry'
            counts['pending'] += 1
        else:
            item['status'] = 'sent'
            counts['sent'] += 1
        save(path, item)
    return counts


def recover_interruption(root, env, result):
    from finance_jobs import Jobs
    from finance_recovery import policy, retry_delay
    if result in {'success', 'exit-code'}:
        return
    root = pathlib.Path(root)
    rules = policy(env)
    jobs = Jobs(root / 'jobs.sqlite')
    interrupted = [jobs.get(row[0]) for row in jobs.db.execute("SELECT video_id FROM jobs WHERE status='processing'")]
    for job in interrupted:
        exhausted = job['attempts'] >= rules['max_attempts']
        if rules['enabled'] and not exhausted:
            jobs.save(job['video_id'], status='retry', auto_recovery_v2=True,
                      next_retry_at=time.time()+retry_delay(job['attempts']), recovery_error='ServiceInterrupted')
        else:
            if exhausted:
                jobs.save(job['video_id'], status='needs_review', recovery_exhausted=True)
            enqueue(root, job['source'], job['checkpoint'].get('stage', '处理视频'),
                    RuntimeError('Service interrupted'), '进程异常中断；自动恢复次数已用尽，需核查' if exhausted else '下次扫描从检查点恢复')
    jobs.db.close()
    if not interrupted:
        if rules['enabled']:
            path = root / 'scan-retry.json'
            previous = json.loads(path.read_text()) if path.exists() else {}
            attempts = previous.get('attempts', 0) + 1
            terminal = attempts >= rules['max_attempts']
            save(path, {'attempts': attempts, 'terminal': terminal,
                        'next_retry_at': time.time()+retry_delay(attempts), 'error_type': 'ServiceInterrupted'})
            if not terminal:
                return
        enqueue(root, {}, '服务运行', RuntimeError('Service interrupted'), '服务连续异常中断，需检查服务器')


if __name__ == '__main__':
    import argparse
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, type=pathlib.Path)
    args = parser.parse_args()
    result = os.environ.get('SERVICE_RESULT', 'success')
    recover_interruption(args.data, os.environ, result)
    scan_missing_reports(args.data)
    print(json.dumps(flush(args.data, os.environ)))
