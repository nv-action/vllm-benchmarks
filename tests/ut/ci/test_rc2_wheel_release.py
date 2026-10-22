# SPDX-License-Identifier: Apache-2.0
"""CPU-only regression tests for A3 OpenMP repair and raw wheel retention."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".github/workflows/scripts/wheel"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOpenMPRepair(unittest.TestCase):
    def setUp(self):
        self.repair = load_script("repair_a3_wheel")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def fake_elf(self, soname="libgomp.so.1"):
        tag = SimpleNamespace(entry=SimpleNamespace(d_tag="DT_SONAME"), soname=soname)
        dynamic = SimpleNamespace(iter_tags=lambda: iter([tag]))
        return SimpleNamespace(get_section_by_name=lambda _: dynamic)

    def test_gcc_linker_script_resolves_runtime_not_script(self):
        runtime = self.root / "libgomp.so.1.0.0"
        runtime.write_bytes(b"\x7fELF runtime")
        link = self.root / "libgomp.so.1"
        link.symlink_to(runtime.name)
        script = self.root / "libgomp.so"
        script.write_text(f"/* GNU ld script */\nOUTPUT_FORMAT(elf64-x86-64)\nINPUT ( {link} )\n")
        with patch.object(self.repair, "ELFFile", return_value=self.fake_elf()):
            self.assertEqual(self.repair.resolve_openmp_runtime(script), runtime)
            self.assertEqual(self.repair.resolve_openmp_runtime(link), runtime)

    def test_non_elf_or_unknown_linker_input_is_rejected(self):
        script = self.root / "libgomp.so"
        script.write_text("INPUT ( -lgomp )")
        with self.assertRaisesRegex(RuntimeError, "Unsupported OpenMP"):
            self.repair.resolve_openmp_runtime(script)
        runtime = self.root / "libgomp.so.1"
        runtime.write_text("not an ELF runtime")
        script.write_text(f"INPUT ( {runtime} )")
        with self.assertRaisesRegex(RuntimeError, "not ELF"):
            self.repair.resolve_openmp_runtime(script)

    def test_wrong_runtime_soname_is_rejected(self):
        runtime = self.root / "libgomp.so.1"
        runtime.write_bytes(b"\x7fELF runtime")
        with (
            patch.object(self.repair, "ELFFile", return_value=self.fake_elf("libother.so")),
            self.assertRaisesRegex(RuntimeError, "SONAME"),
        ):
            self.repair.resolve_openmp_runtime(runtime)

    def test_auditwheel_sbom_is_allowed_but_extra_libraries_are_not(self):
        original = {"vllm_ascend/vllm_ascend_C.so", "vllm_ascend-0.30.0rc2.dist-info/METADATA"}
        runtime = "vllm_ascend.libs/libgomp-12345678.so.1"
        files = original | {runtime, "vllm_ascend-0.30.0rc2.dist-info/sboms/auditwheel.cdx.json"}
        self.assertEqual(self.repair.bundled_openmp(original, files, "12345678"), runtime)
        with self.assertRaisesRegex(RuntimeError, "different OpenMP"):
            self.repair.bundled_openmp(original, files, "87654321")
        with self.assertRaisesRegex(RuntimeError, "Unexpected payload"):
            self.repair.bundled_openmp(original, files | {"vllm_ascend.libs/libother.so"}, "12345678")
        with self.assertRaisesRegex(RuntimeError, "Unexpected payload"):
            self.repair.bundled_openmp(original, files - {"vllm_ascend/vllm_ascend_C.so"}, "12345678")

    def test_platform_tags_accept_multiple_compatible_tags_reject_238(self):
        self.repair.check_repaired_tags(
            Path("pkg-1-cp311-cp311-manylinux_2_28_x86_64.manylinux_2_34_x86_64.whl"), "x86_64"
        )
        self.repair.check_repaired_tags(
            Path("pkg-1-cp311-cp311-manylinux2014_x86_64.manylinux_2_34_x86_64.whl"), "x86_64"
        )
        for tag in ("manylinux_2_38_x86_64", "manylinux_2_34_aarch64", "linux_x86_64"):
            with self.assertRaises(RuntimeError):
                self.repair.check_repaired_tags(Path(f"pkg-1-cp311-cp311-{tag}.whl"), "x86_64")

    def test_glibc_238_symbol_requirement_is_rejected(self):
        needs = SimpleNamespace(iter_versions=lambda: [(None, [SimpleNamespace(name="GLIBC_2.38")])])
        elf = SimpleNamespace(get_section_by_name=lambda _: needs)
        with (
            patch.object(self.repair, "ELFFile", return_value=elf),
            self.assertRaisesRegex(RuntimeError, "unsupported GLIBC_2.38"),
        ):
            self.repair.check_glibc("libgomp", b"fake elf")


class TestWorkflowSafety(unittest.TestCase):
    def test_a3_only_saves_raw_wheel_without_cancelling_or_publishing(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/schedule_release_code_and_wheel.yml").read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["publish"]["default"])
        self.assertIn("a3_only", inputs)
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertIn("github.run_id", workflow["concurrency"]["group"])
        jobs = workflow["jobs"]
        a3 = jobs["build_and_release_wheel_a3"]
        self.assertFalse(a3["strategy"]["fail-fast"])
        self.assertEqual(a3["if"], "${{ !inputs.publish_run_id && !inputs.a2_only }}")
        names = [step["name"] for step in a3["steps"]]
        raw_index = names.index("Archive raw wheel before repair")
        self.assertLess(names.index("Build wheel"), raw_index)
        self.assertLess(raw_index, names.index("Repair wheels with auditwheel"))
        raw_upload = a3["steps"][raw_index]
        self.assertEqual(raw_upload["with"]["path"], "dist/*.whl")
        self.assertEqual(raw_upload["with"]["retention-days"], 90)
        for name in (
            "build_and_release_code",
            "build_and_release_wheel",
            "build_and_release_wheel_310p",
            "build_and_release_wheel_a5",
            "generate_and_upload_variant_index",
        ):
            self.assertIn("!inputs.a3_only", jobs[name]["if"])
        for name, job in jobs.items():
            if name.startswith("build_and_release_"):
                self.assertIn("!inputs.publish_run_id", job["if"])
        publish = jobs["generate_and_upload_variant_index"]
        self.assertIn("inputs.publish_run_id ||", publish["if"])
        self.assertFalse(publish["concurrency"]["cancel-in-progress"])
        download = next(step for step in publish["steps"] if step["name"] == "Download all variant wheels")
        self.assertEqual(download["with"]["run-id"], "${{ inputs.publish_run_id || github.run_id }}")
        self.assertFalse(download["with"]["merge-multiple"])
        dockerfile = (ROOT / ".github/workflows/dockerfiles/Dockerfile.buildwheel.a3").read_text()
        self.assertNotIn("check-runtime-only", dockerfile)
        self.assertNotIn("pyelftools", dockerfile)


if __name__ == "__main__":
    unittest.main()
