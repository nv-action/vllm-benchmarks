"""Small synthetic checks for the YCSB adapter; no benchmark data is created."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("run_ycsb.py")
SPEC = importlib.util.spec_from_file_location("rocksdb_run_ycsb", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RunYcsbTest(unittest.TestCase):
    def test_load_warmup_and_two_formal_workloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            distribution = work / "ycsb-dist"
            (distribution / "bin").mkdir(parents=True)
            (distribution / "workloads").mkdir()
            (distribution / "lib").mkdir()
            (distribution / "lib" / "rocksdbjni-6.26.1.jar").write_bytes(b"fixture")
            for name in ("workloada", "workloadc"):
                (distribution / "workloads" / name).write_text(name)
            (distribution / "bin" / "ycsb").write_text(
                "import pathlib,sys\n"
                "args=sys.argv[1:]\n"
                "db=pathlib.Path(next(x.split('=',1)[1] for x in args if x.startswith('rocksdb.dir=')))\n"
                "db.mkdir(parents=True,exist_ok=True)\n"
                "(db/'record').write_text('fixture')\n"
                "print('[OVERALL], Throughput(ops/sec), 200.5')\n"
            )
            result = work / "result.json"
            raw = work / "raw.log"
            environ = {
                "PERF_WORK_DIR": str(work), "ROCKSDB_BENCH_DATA_DIR": str(work / "data"),
                "SOFTWARE_VERSION": "6.26.1", "EXPECTED_ARCH": "x86_64",
            }
            with patch.dict(os.environ, environ), patch.object(MODULE.subprocess, "run", return_value=SimpleNamespace(stderr='openjdk version "11.0.1"\n')), patch.object(
                MODULE.sys, "argv", [str(SCRIPT), str(distribution), str(result), str(raw)]
            ):
                self.assertEqual(MODULE.main(), 0)
            payload = json.loads(result.read_text())
            self.assertEqual(set(payload["results"]), {"workloada throughput", "workloadc throughput"})
            self.assertTrue(all(row["value"] == 200.5 for row in payload["results"].values()))
            self.assertEqual(raw.read_text().count("=== formal "), 2)
            self.assertNotIn("rocksdb.optionsfile=", raw.read_text())
            self.assertTrue((work / "data" / "warmup_workload_data" / "record").is_file())


if __name__ == "__main__":
    unittest.main()
