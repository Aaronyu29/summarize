"""Actual response usage and versioned tariff estimates, never invoice claims."""
import datetime as dt
import hashlib
import json
import pathlib
from decimal import Decimal

PRICE = {'model': 'deepseek-flash', 'currency': 'CNY', 'checked_date': '2026-10-23',
         'source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
         'offpeak_per_million': {'cache_hit': '0.02', 'cache_miss': '1', 'output': '4'},
         'peak_multiplier': 2,
         'basis': '官方价折算，非账户账单；工作日高峰按北京时间09–12、14–18；法定节假日需价格配置指定'}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def summarize(capture):
    capture = pathlib.Path(capture)
    config_path = capture.parent.parent / 'cost-pricing.json'
    price = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else PRICE
    rates = price['offpeak_per_million']
    calls, seen, unknown = [], set(), []
    for path in sorted((capture / capture.name).glob('*.response.json')):
        raw = path.read_bytes()
        response = json.loads(raw)
        identity = response.get('id') or hashlib.sha256(raw).hexdigest()
        if identity in seen:
            continue
        seen.add(identity)
        usage = response.get('usage', {})
        prompt, output = usage.get('prompt_tokens'), usage.get('completion_tokens')
        hit = usage.get('prompt_cache_hit_tokens', usage.get('prompt_tokens_details', {}).get('cached_tokens'))
        created = response.get('created')
        if (not all(isinstance(v, int) and v >= 0 for v in [prompt, output, hit]) or
                hit > prompt or not created or response.get('model') != price['model']):
            unknown.append(path.name)
            continue
        miss = prompt - hit
        if usage.get('prompt_cache_miss_tokens', miss) != miss or usage.get('total_tokens', prompt + output) != prompt + output:
            unknown.append(path.name)
            continue
        moment = dt.datetime.fromtimestamp(created, dt.timezone(dt.timedelta(hours=8)))
        peak = (moment.weekday() < 5 and moment.date().isoformat() not in price.get('holidays', []) and
                (9 <= moment.hour < 12 or 14 <= moment.hour < 18))
        base = (Decimal(hit)*Decimal(rates['cache_hit']) + Decimal(miss)*Decimal(rates['cache_miss']) +
                Decimal(output)*Decimal(rates['output'])) / Decimal(1000000)
        calls.append({'response_id': identity, 'response_file': path.name, 'model': response['model'],
                      'response_sha256': hashlib.sha256(raw).hexdigest(), 'created_shanghai': moment.isoformat(),
                      'prompt_tokens': prompt, 'cache_hit_tokens': hit, 'cache_miss_tokens': miss,
                      'completion_tokens': output, 'total_tokens': prompt+output,
                      'reasoning_tokens': usage.get('completion_tokens_details', {}).get('reasoning_tokens'),
                      'period': 'peak' if peak else 'offpeak',
                      'estimated_cost_cny': str(base * (price['peak_multiplier'] if peak else 1))})
    keys = ['prompt_tokens', 'cache_hit_tokens', 'cache_miss_tokens', 'completion_tokens', 'total_tokens']
    totals = {key: sum(c[key] for c in calls) for key in keys}
    totals['estimated_cost_cny'] = str(sum((Decimal(c['estimated_cost_cny']) for c in calls), Decimal(0)))
    analysis = capture / 'analysis-v1.json'
    published = None
    if analysis.exists():
        videos = json.loads(analysis.read_text(encoding='utf-8'))['videos']
        published = next((v.get('published_shanghai') for v in videos if v['video_id'] == capture.name), None)
    result = {'schema_version': 1, 'video_id': capture.name, 'published_shanghai': published,
              'price_snapshot': price, 'calls': calls, 'totals': totals, 'call_count': len(calls),
              'unknown_usage_files': unknown,
              'unresolved_requests': [p.name for p in (capture/capture.name).glob('*.pending.json')],
              'scope': '包括画面分析、初稿、修订及所有审核；reasoning 已包含在 completion 中，不重复计费；不含服务器成本和独立预检调用'}
    result['usage_complete'] = not result['unknown_usage_files'] and not result['unresolved_requests']
    save(capture/'cost-summary.json', result)
    return result


def update(capture):
    result = summarize(capture)
    root = pathlib.Path(capture).parent.parent
    videos = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((root/'videos').glob('*/cost-summary.json'))]
    save(root/'cost-ledger.json', {'schema_version': 1, 'videos': [
        {k: v[k] for k in ['video_id', 'published_shanghai', 'call_count', 'totals', 'usage_complete']} for v in videos],
        'estimated_cost_cny': str(sum((Decimal(v['totals']['estimated_cost_cny']) for v in videos), Decimal(0)))})
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, type=pathlib.Path)
    args = parser.parse_args()
    for capture in sorted((args.data/'videos').iterdir()):
        if capture.is_dir() and (capture/capture.name).is_dir():
            value = update(capture)
            print(json.dumps({k: value[k] for k in ['video_id', 'call_count', 'totals', 'usage_complete']}))
