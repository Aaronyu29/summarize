#!/usr/bin/env bash
set -euo pipefail
# Run from the downloaded CI artifact; never build application code on the host.
if [[ $# != 2 || "$1" != "--execute" ]]; then
  printf '%s\n' 'Preview: install.sh --execute FULL_COMMIT_SHA verifies and installs CI wheels offline. Does not activate services.'
  exit 0
fi
artifact_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$artifact_dir" "$2" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
expected=sys.argv[2]
assert re.fullmatch(r'[0-9a-f]{40}',expected), 'Full revision required'
manifest=json.loads((root/'manifest.json').read_text())
assert manifest['revision']==expected, 'Release revision mismatch'
assert manifest['akshare_revision']=='cc7e451d7ba372596a70131121323a8b5f15ee73'
assert sys.version_info[:2]==(3,12), 'Release requires Python 3.12'
for relative,digest in manifest['files'].items():
    path=(root/relative).resolve()
    assert path.is_relative_to(root) and path.is_file(), 'Invalid artifact path'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest, 'Artifact checksum mismatch'
assert {p.relative_to(root).as_posix() for p in (root/'wheels').glob('*')} == {p for p in manifest['files'] if p.startswith('wheels/')}
print('CI revision and file checksums verified')
PY
python3 -m venv "$artifact_dir/venv"
"$artifact_dir/venv/bin/python" -m pip install --no-index --no-deps "$artifact_dir"/wheels/*.whl
"$artifact_dir/venv/bin/python" -m pip check
"$artifact_dir/venv/bin/python" -c 'import rhino_invest, stock_alert.market_quotes, longbridge; print("CI application and market adapter imports verified")'
printf '%s\n' 'Offline installation complete. Activation is a separate deployment step.'
