"""Seal the CI-built wheelhouse and deployment files for offline installation."""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--directory', default='release')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.revision):
        raise ValueError('A full Git commit is required')
    root = Path(args.directory)
    wheels = list((root / 'wheels').glob('*.whl'))
    if not wheels or not any(p.name.startswith('rhino_invest-') for p in wheels):
        raise ValueError('CI must build application and dependency wheels first')
    for name in ('install.sh', 'rhino-invest.service'):
        shutil.copyfile(Path(__file__).parent / name, root / name)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'manifest.json'}
    manifest = {'revision': args.revision, 'python': '3.12', 'platform': 'linux-x86_64',
                'akshare_revision': 'cc7e451d7ba372596a70131121323a8b5f15ee73',
                'files': files}
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
