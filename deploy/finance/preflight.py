"""Read credentials in-process and verify cloud vision, ASR and media reachability.

Only fixed public sample evidence is used. No summary is regenerated or delivered.
"""
import base64
import json
import pathlib
import resource
import sys
import time
import urllib.request

root = pathlib.Path('/opt/rhino-video-digest')
env = {}
for line in pathlib.Path('/etc/rhino-video-digest.env').read_text().splitlines():
    key, _, value = line.partition('=')
    env[key] = json.loads(value)

import requests
frame = root / 'data/frame-00325.jpg'
body = {'model': env['LLM_MODEL'], 'thinking': {'type': 'disabled'}, 'max_tokens': 500,
        'response_format': {'type': 'json_object'}, 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Read the stock ticker visible in this screenshot. Return JSON with ticker and readable boolean. This is an interface test, not an investment analysis.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(frame.read_bytes()).decode()}}]}]}
response = requests.post(env['LLM_BASE_URL'].rstrip('/') + '/chat/completions', json=body,
    headers={'Authorization': 'Bearer ' + env['LLM_API_KEY']}, timeout=(15, 120))
if response.status_code != 200:
    raise RuntimeError(f'Vision API HTTP {response.status_code}; body suppressed')
result = response.json()
vision = json.loads(result['choices'][0]['message']['content'])
if vision.get('readable') is not True or vision.get('ticker', '').upper() != 'LRCX':
    raise ValueError('Vision result did not match the known public chart')

from faster_whisper import WhisperModel
started = time.monotonic()
model = WhisperModel(env['ASR_MODEL'], device='cpu', compute_type='int8', cpu_threads=1)
segments, info = model.transcribe(str(root / 'data/cloud-asr-probe.m4a'), language='zh',
    beam_size=5, vad_filter=False, condition_on_previous_text=False)
segments = list(segments)
if not segments or max(s.end for s in segments) < 15:
    raise ValueError('ASR did not transcribe the probe')
elapsed = time.monotonic() - started
core = {'vision': vision, 'model': result['model'], 'usage': result.get('usage'),
        'asr_seconds': round(elapsed, 2), 'audio_seconds': info.duration,
        'asr_peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
(root / 'data/preflight-core.json').write_text(json.dumps(core, ensure_ascii=False, indent=2))
print(json.dumps(core, ensure_ascii=False), flush=True)
if '--core-only' in sys.argv:
    raise SystemExit(0)

from yt_dlp import YoutubeDL
with YoutubeDL({'quiet': True, 'socket_timeout': 30}) as ydl:
    metadata = ydl.extract_info('https://www.youtube.com/watch?v=DQ0Yn27pdgk', download=False)
audio = [f for f in metadata['formats'] if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
if not audio:
    raise ValueError('No downloadable audio format')
request = urllib.request.Request(audio[-1]['url'], headers={**audio[-1].get('http_headers', {}), 'Range': 'bytes=0-65535'})
with urllib.request.urlopen(request, timeout=30) as media:
    received = len(media.read(65536))
    media_status = media.status
if received < 1024:
    raise ValueError('Media probe did not return playable bytes')
evidence = {'vision': vision, 'model': result['model'], 'usage': result.get('usage'),
            'asr_seconds': round(elapsed, 2), 'audio_seconds': info.duration,
            'asr_peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            'media_http': media_status, 'media_bytes': received}
(root / 'data/preflight.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
print(json.dumps(evidence, ensure_ascii=False))
