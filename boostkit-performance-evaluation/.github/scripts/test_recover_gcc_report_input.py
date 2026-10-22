import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("recover_gcc_report_input.py")
SPEC = importlib.util.spec_from_file_location("recover_gcc_report_input", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RecoverGccReportInputTest(unittest.TestCase):
    def test_preserves_measurements_and_aligns_only_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arch, config in (("x86_64", "spec-gcc-x86.cfg"), ("aarch64", "spec-gcc-aarch64.cfg")):
                folder = root / "Toolchain" / "gcc" / "14.4.0" / arch
                folder.mkdir(parents=True)
                source = {
                    "parameters": {
                        "command": ["runcpu", f"--config={config}", "intrate", "-n", "1", "-C", "384"],
                        "suite": "SPEC CPU2017 intrate", "copies": 384, "iterations": 1,
                    },
                    "results": {"SPECrate2017_int_base": {"value": 123.4}},
                }
                (folder / "benchmark_gcc.json").write_text(json.dumps(source), encoding="utf-8")
                params = {"benchmark_result": source["parameters"]}
                result = {
                    "category": "Toolchain", "software": "gcc", "version": "14.4.0",
                    "architecture": arch, "run_id": "38199750494-2", "status": "passed",
                    "cleanup_status": "passed", "parameters": params,
                    "parameter_signature": MODULE.signature(params),
                    "sources": {"benchmark_result": source},
                    "metrics": {"SPECrate2017_int_base": {"value": 123.4}},
                }
                (folder / "normalized_result.json").write_text(json.dumps(result), encoding="utf-8")
            MODULE.recover(root, "38199750494-2", "38199750494-3")
            recovered = [json.loads(path.read_text()) for path in root.rglob("normalized_result.json")]
            self.assertEqual(recovered[0]["parameters"], recovered[1]["parameters"])
            self.assertEqual(recovered[0]["parameter_signature"], recovered[1]["parameter_signature"])
            for result in recovered:
                self.assertEqual(result["metrics"]["SPECrate2017_int_base"]["value"], 123.4)
                self.assertEqual(result["run_id"], "38199750494-3")
            self.assertEqual(len(list(root.rglob("normalized_result.original.json"))), 2)


if __name__ == "__main__":
    unittest.main()
