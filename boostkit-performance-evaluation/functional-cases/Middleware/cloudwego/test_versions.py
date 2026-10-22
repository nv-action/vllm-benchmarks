"""Regression coverage for independent component and CLI versions."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

COMMON = Path(__file__).with_name('common.sh')


class VersionTests(unittest.TestCase):
    def verify(self, versions, eino='v0.9.22'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, version in zip(('kitex', 'hz', 'volo'), versions):
                path = root / name
                path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} {version}"\n')
                path.chmod(0o755)
            (root / '.cloudwego-version').write_text(eino + '\n')
            env = {**os.environ, 'SOFTWARE_VERSION': '0.16.4', 'RESULTS_DIR': tmp}
            result = subprocess.run([
                'bash', '-c', 'source "$1"; KITEX_BIN="$2/kitex"; HZ_BIN="$2/hz"; '
                'VOLO_BIN="$2/volo"; EINO_SOURCE_DIR="$2"; verify_components',
                'test', str(COMMON), tmp], env=env, capture_output=True, text=True)
            return result.returncode

    def test_matching_cli_versions(self):
        self.assertEqual(self.verify(('0.16.3', '0.9.7', '0.12.1')), 0)

    def test_wrong_component_cli(self):
        for versions in (('0.16.4', '0.9.7', '0.12.1'),
                         ('0.16.3', '0.10.6', '0.12.1'),
                         ('0.16.3', '0.9.7', '0.12.2')):
            with self.subTest(versions=versions):
                self.assertNotEqual(self.verify(versions), 0)

    def test_wrong_eino_source(self):
        self.assertNotEqual(self.verify(('0.16.3', '0.9.7', '0.12.1'), 'v0.9.21'), 0)


if __name__ == '__main__':
    unittest.main()
