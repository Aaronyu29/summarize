"""The unified CI artifact must include the report generator as well as investment wheels."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class UnifiedReleaseTests(unittest.TestCase):
    def test_ci_manifest_covers_both_services_without_private_data(self):
        project = Path(__file__).resolve().parents[1]
        scratch = project / '.cache' / 'finance-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            release = Path(temp)
            (release / 'wheels').mkdir()
            (release / 'wheels/rhino_invest-0.1.0-py3-none-any.whl').write_bytes(b'packaging-fixture')
            revision = 'a' * 40
            subprocess.run([sys.executable, str(project / 'deploy/investment/build_release.py'),
                            '--revision', revision, '--directory', str(release)], check=True, cwd=project)
            manifest = json.loads((release / 'manifest.json').read_text())
            self.assertEqual(manifest['revision'], revision)
            for name in ('finance_archive.py', 'finance_cloud.py', 'finance_feishu.py'):
                path = 'finance/scripts/' + name
                self.assertEqual(manifest['files'][path], hashlib.sha256((project / 'scripts' / name).read_bytes()).hexdigest())
                self.assertEqual((release / path).read_bytes(), (project / 'scripts' / name).read_bytes())
            self.assertIn('finance/scripts/test_finance_feishu.py', manifest['files'])
            self.assertIn('wheels/rhino_invest-0.1.0-py3-none-any.whl', manifest['files'])
            self.assertFalse(any('research-data' in path or '.env' in path for path in manifest['files']))


if __name__ == '__main__':
    unittest.main()
