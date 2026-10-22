# SPDX-License-Identifier: Apache-2.0
"""Publication checks run on CPUs with the release tooling dependencies."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[3] / ".github/workflows/scripts/wheel"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"repair_a3_wheel": load_repair()}):
        spec.loader.exec_module(module)
    return module


def load_repair():
    spec = importlib.util.spec_from_file_location("repair_a3_wheel", SCRIPTS / "repair_a3_wheel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def index_data(labels):
    return {
        "$schema": "https://variants-schema.wheelnext.dev/v0.0.3.json",
        "default-priorities": {"namespace": ["ascend"]},
        "providers": {"ascend": {"requires": ["huawei-ascend-variant-provider>=0.0.2,<0.0.3"]}},
        "variants": {label: {"ascend": {"npu_type": [label]}} for label in labels},
    }


class TestExistingPublication(unittest.TestCase):
    def setUp(self):
        self.publish = load_script("publish_existing_variants")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.index = self.directory / f"{self.publish.NAMEVER}-variants.json"
        self.make_wheels(self.publish.LABELS)

    def make_wheels(self, labels):
        self.index.write_text(json.dumps(index_data(labels)))
        for label, python, arch in product(labels, self.publish.PYTHONS, self.publish.ARCHITECTURES):
            platform = f"manylinux_2_34_{arch}"
            wheel = self.directory / f"{self.publish.NAMEVER}-{python}-{python}-{platform}-{label}.whl"
            info = f"{self.publish.NAMEVER}.dist-info"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(f"{info}/METADATA", "Name: vllm-ascend\nVersion: 0.30.0rc2\n")
                archive.writestr(f"{info}/WHEEL", f"Tag: {python}-{python}-{platform}\n")
                archive.writestr(f"{info}/variant.json", json.dumps(index_data([label])))

    def validate(self, labels=None):
        with redirect_stdout(io.StringIO()):
            return self.publish.validate(self.directory, self.publish.LABELS if labels is None else labels)

    def test_a3_matrix_requires_all_six_repaired_variants(self):
        for wheel in self.directory.glob("*.whl"):
            wheel.unlink()
        self.make_wheels(("a3",))
        self.assertEqual(len(self.validate(("a3",))), 6)
        with self.assertRaisesRegex(ValueError, "Unexpected or duplicate"):
            self.validate()
        next(self.directory.glob("*.whl")).unlink()
        with self.assertRaisesRegex(ValueError, "Missing release"):
            self.validate(("a3",))

    def test_complete_matrix_and_missing_combination(self):
        self.assertEqual(len(self.validate()), 18)
        next(self.directory.glob("*.whl")).unlink()
        with self.assertRaisesRegex(ValueError, "Missing release"):
            self.validate()

    def test_unexpected_variant_is_rejected(self):
        wheel = next(self.directory.glob("*-950.whl"))
        wheel.rename(wheel.with_name(wheel.name.replace("-950.whl", "-a3.whl")))
        with self.assertRaisesRegex(ValueError, "Unexpected or duplicate"):
            self.validate()

    def test_glibc_238_tag_is_rejected(self):
        wheel = next(self.directory.glob("*.whl"))
        wheel.rename(wheel.with_name(wheel.name.replace("manylinux_2_34", "manylinux_2_38")))
        with self.assertRaisesRegex(RuntimeError, "platform tag"):
            self.validate()

    def test_metadata_tags_must_match_filename(self):
        wheel = next(self.directory.glob("*-cp310-cp310-*.whl"))
        # Keep a supported filename while making it disagree with WHEEL metadata.
        other = wheel.with_name(wheel.name.replace("cp310", "cp311"))
        other.unlink()
        wheel.rename(other)
        with self.assertRaisesRegex(ValueError, "metadata tags mismatch"):
            self.validate()

    def test_merge_retains_existing_variant_and_rejects_conflicts(self):
        previous = index_data(["a3"])
        merged = self.publish.merge_index(self.index.read_text(), json.dumps(previous))
        self.assertEqual(set(json.loads(merged)["variants"]), {"910b", "950", "310p", "a3"})
        previous["variants"]["950"] = previous["variants"]["a3"]
        with self.assertRaises(ValueError):
            self.publish.merge_index(self.index.read_text(), json.dumps(previous))

    def test_a3_index_merge_preserves_published_device_entries(self):
        previous = self.index.read_text()
        current = json.dumps(index_data(["a3"]))
        merged = json.loads(self.publish.merge_index(current, previous))
        self.assertEqual(set(merged["variants"]), {"910b", "950", "310p", "a3"})
        for label in self.publish.LABELS:
            self.assertEqual(merged["variants"][label], json.loads(previous)["variants"][label])

    def test_upload_wheels_before_index_and_verify_readback(self):
        wheels = self.validate()
        client = Mock()
        stored = {}

        def get_object(bucket, key, **kwargs):
            if key in stored:
                return SimpleNamespace(status=200, body=SimpleNamespace(buffer=stored[key]))
            return SimpleNamespace(status=404)

        def put_file(bucket, key, path):
            stored[key] = Path(path).read_bytes()
            return SimpleNamespace(status=200)

        client.getObject.side_effect = get_object
        client.putFile.side_effect = put_file
        client.getObjectMetadata.side_effect = lambda bucket, key: SimpleNamespace(
            status=200, body=SimpleNamespace(contentLength=len(stored[key]))
        )
        with redirect_stdout(io.StringIO()):
            self.publish.publish(client, self.directory, wheels)
        self.assertEqual(len(stored), 19)
        self.assertTrue(list(stored)[-1].endswith("-variants.json"))

    def test_failed_index_read_or_wheel_upload_never_publishes_index(self):
        wheels = self.validate()
        client = Mock()
        client.getObject.return_value = SimpleNamespace(status=403, errorMessage="denied")
        with self.assertRaisesRegex(RuntimeError, "Cannot read"):
            self.publish.publish(client, self.directory, wheels)
        client.putFile.assert_not_called()
        client.getObject.return_value = SimpleNamespace(status=404)
        client.putFile.return_value = SimpleNamespace(status=500, errorMessage="failed")
        with self.assertRaisesRegex(RuntimeError, "Upload failed"):
            self.publish.publish(client, self.directory, wheels)
        self.assertEqual(client.putFile.call_count, 1)
        self.assertTrue(client.putFile.call_args.args[1].endswith(".whl"))


if __name__ == "__main__":
    unittest.main()
