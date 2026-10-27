"""Durable, deduplicated Lark failure notices; never include raw exceptions."""
import datetime as dt
import hashlib
import json
import pathlib
import re
import time

from finance_feishu import signature


def reason(error):
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


def enqueue(root, source, stage, error, action):
    description = reason(error)
    identity = hashlib.sha256(json.dumps([source.get('video_id'), stage, description, action],
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


if __name__ == '__main__':
    import argparse
    import os
    from finance_jobs import Jobs
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, type=pathlib.Path)
    args = parser.parse_args()
    result = os.environ.get('SERVICE_RESULT', 'success')
    if result not in {'success', 'exit-code'}:
        jobs = Jobs(args.data / 'jobs.sqlite')
        interrupted = [jobs.get(row[0]) for row in jobs.db.execute("SELECT video_id FROM jobs WHERE status='processing'")]
        for job in interrupted:
            enqueue(args.data, job['source'], job['checkpoint'].get('stage', '处理视频'),
                    RuntimeError('Service interrupted'), '进程异常中断（超时、信号或资源限制），下次扫描从检查点恢复')
        if not interrupted:
            enqueue(args.data, {}, '服务运行', RuntimeError('Service interrupted'), '服务异常中断，需检查服务器；下次定时扫描重试')
    print(json.dumps(flush(args.data, os.environ)))
