import json
from pathlib import Path
import tempfile
import unittest
from verify_source import verify


class SourceLockTests(unittest.TestCase):
    def test_unknown_tag_rejected_before_read(self):
        with self.assertRaises(ValueError):
            verify('snappy', '1.1.8', '/does/not/exist')

    def test_tampered_archives_rejected(self):
        lock = json.loads(Path(__file__).with_name('source-lock.json').read_text())
        with tempfile.NamedTemporaryFile() as file:
            file.write(b'corrupt archive')
            file.flush()
            for component, entry in lock.items():
                with self.subTest(component=component), self.assertRaises(ValueError):
                    verify(component, entry['tag'], file.name)


if __name__ == '__main__':
    unittest.main()
