import importlib.util
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("write_maven_settings.py")
SPEC = importlib.util.spec_from_file_location("rocksdb_write_maven_settings", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MavenSettingsTest(unittest.TestCase):
    def test_direct_and_private_proxy_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "settings.xml"
            with patch.object(MODULE.sys, "argv", [str(SCRIPT), str(output)]):
                with patch.dict(os.environ, {"PERF_PROXY": ""}):
                    self.assertEqual(MODULE.main(), 0)
                self.assertIsNone(ET.parse(output).getroot().find("{*}proxies"))
                with patch.dict(os.environ, {"PERF_PROXY": "http://user:secret@example.com:8080"}):
                    self.assertEqual(MODULE.main(), 0)
            root = ET.parse(output).getroot()
            self.assertEqual(root.findtext(".//{*}host"), "example.com")
            self.assertEqual(root.findtext(".//{*}username"), "user")
            self.assertEqual(root.findtext(".//{*}password"), "secret")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
