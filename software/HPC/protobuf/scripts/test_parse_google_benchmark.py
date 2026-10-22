"""Small schema checks for the original protobuf benchmark JSON adapter."""

import json
import itertools
import unittest
from pathlib import Path
from unittest.mock import patch

from parse_google_benchmark import GROUPS, normalize
from standalone_runtime import extract_metrics, render_report


class ParserTests(unittest.TestCase):
    def setUp(self):
        kinds = {
            "Scalar": ("Int32", "UInt32", "SInt32", "Int64", "UInt64", "SInt64", "Bool", "Enum", "Double", "Float"),
            "Repeated": ("Int32", "UInt32", "SInt32", "Int64", "UInt64", "SInt64", "Double", "Float"),
            "String": ("ASCII", "Chinese"),
            "Bytes": ("",),
        }
        self.names = [
            f"BM_{operation}_{kind}{'_' + suffix if suffix else ''}/{size}"
            for operation, (kind, suffix), size in itertools.product(
                ("Serialize", "Deserialize"),
                ((kind, suffix) for kind, suffixes in kinds.items() for suffix in suffixes),
                (10, 100, 1000, 10000),
            )
        ]
        self.rows = [
            {
                "run_type": "iteration",
                "run_name": f"{name}/repeats:5",
                "cpu_time": repeat + 1,
                "time_unit": "us",
            }
            for name in self.names
            for repeat in range(5)
        ]

    def parse(self):
        with patch.object(Path, "read_text", side_effect=[
            "\n".join(self.names), json.dumps({"benchmarks": self.rows})
        ]):
            return normalize(Path("benchmark_google.json"), Path("benchmark_cases.txt"))

    def test_collects_all_cases_and_converts_to_nanoseconds(self):
        result = self.parse()
        self.assertEqual(len(result["results"]), 168)
        self.assertEqual(result["results"][self.names[0]]["value"], 3000.0)
        self.assertEqual(set(item["group"] for item in result["results"].values()), set(GROUPS))
        self.assertEqual(result["results"][self.names[0]]["group"], GROUPS[0])

    def test_rejects_missing_repetition(self):
        self.rows.pop()
        with self.assertRaisesRegex(ValueError, "missing or extra repetitions"):
            self.parse()

    def test_standalone_report_has_one_table_per_group(self):
        normalized = self.parse()
        metrics = extract_metrics(normalized, "33.0", "x86_64")
        report = render_report({
            "version": "33.0", "run_id": "test", "architecture": "x86_64",
            "status": "passed", "cleanup_status": "passed", "metrics": metrics,
        })
        for group in GROUPS:
            self.assertIn(f"### {group}\n", report)
        self.assertEqual(report.count("| 指标 | 数值 | 单位 | 优化方向 |"), 6)


if __name__ == "__main__":
    unittest.main()
