"""Run with sudo remotely; receive authorized Feishu settings on encrypted SSH stdin.

Copies only the three model fields from AIHOT; never prints values or alters AIHOT.
"""
import json
import os
import pathlib
import sys

source = {}
for line in pathlib.Path('/opt/aihot/.env').read_text().splitlines():
    key, separator, value = line.partition('=')
    if separator and not key.startswith('#'):
        source[key.strip()] = value.strip().strip('"').strip("'")
received = json.load(sys.stdin)
allowed = {'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_FOLDER_TOKEN',
           'LARK_WEBHOOK_URL', 'LARK_WEBHOOK_SECRET'}
if set(received) - allowed:
    raise ValueError('Unexpected configuration fields')
config = {k: source[k] for k in ['LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL']}
if config['LLM_BASE_URL'].rstrip('/') != 'https://api.deepseek.com/v1' or config['LLM_MODEL'] != 'deepseek-flash':
    raise ValueError('Model configuration differs from verified provider/model')
config.update(received)
config['ASR_MODEL'] = '/opt/rhino-video-digest/models/small'
path = pathlib.Path('/etc/rhino-video-digest.env')
if path.exists():
    raise ValueError('Refusing to overwrite existing credentials')
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    for key, value in config.items():
        if any(c in value for c in ['\n', '\r', '\x00']):
            raise ValueError('Multiline credentials are unsupported')
        stream.write(key + '=' + json.dumps(value) + '\n')
print(json.dumps({'configured_keys': sorted(config), 'mode': oct(path.stat().st_mode & 0o777)}))
