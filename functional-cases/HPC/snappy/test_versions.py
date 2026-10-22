"""Regression coverage for the delivered SnappyConfigVersion.cmake parsing."""
import subprocess
import tempfile
import unittest
from pathlib import Path

COMMON = Path(__file__).with_name('common.sh')

VERSION_FILE_BODY = (
    '# This is a basic version file for the Config-mode of find_package().\n'
    '#\n'
    '# The created file sets PACKAGE_VERSION_EXACT if the current version\n'
    "set(PACKAGE_VERSION \"{version}\")\n"
    '\n'
    '# Check whether the requested PACKAGE_FIND_VERSION is compatible\n'
    'if(PACKAGE_FIND_VERSION_RANGE)\n'
    'endif()\n'
)


def read_version(version):
    with tempfile.TemporaryDirectory() as tmp:
        version_file = Path(tmp) / 'SnappyConfigVersion.cmake'
        version_file.write_text(VERSION_FILE_BODY.format(version=version))
        result = subprocess.run(
            ['bash', '-c', 'source "$1"; read_snappy_version "$2"',
             'test', str(COMMON), str(version_file)],
            capture_output=True, text=True)
        return result.returncode, result.stdout.strip()


class VersionTests(unittest.TestCase):
    def test_parses_reference_version_file(self):
        code, version = read_version('1.1.7')
        self.assertEqual(code, 0)
        self.assertEqual(version, '1.1.7')

    def test_reports_wrong_version_faithfully(self):
        code, version = read_version('9.9.9')
        self.assertEqual(code, 0)
        self.assertEqual(version, '9.9.9')
        self.assertNotEqual(version, '1.1.7')

    def test_version_file_without_package_version_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / 'SnappyConfigVersion.cmake'
            version_file.write_text('# no PACKAGE_VERSION here\n')
            result = subprocess.run(
                ['bash', '-c', 'source "$1"; read_snappy_version "$2"',
                 'test', str(COMMON), str(version_file)],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
