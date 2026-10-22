"""Contract tests for the documented lzbench Snappy output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_benchmark import parse_lzbench


class LzbenchParserTests(unittest.TestCase):
    def test_single_snappy_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                "Compressor name         Compress. Decompress. Compr. size  Ratio Filename\n"
                f"snappy 1.2.2             1107 MB/s   790 MB/s        5  50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=20.0 dTime=20.0 chunkSize=4KB cSpeed=0MB\n"
            )
            results = parse_lzbench(output, corpus)
            self.assertEqual(results["compression_throughput"]["value"], 1107.0)
            self.assertEqual(results["decompression_throughput"]["value"], 790.0)
            self.assertEqual(results["compressed_size_ratio"]["value"], 50.0)

    def test_rejects_changed_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                f"snappy 1.2.2 1107 MB/s 790 MB/s 5 50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=1.0 dTime=1.0 chunkSize=4KB\n"
            )
            with self.assertRaisesRegex(RuntimeError, "parameters"):
                parse_lzbench(output, corpus)


if __name__ == "__main__":
    unittest.main()
