"""Contract tests for the LZ4 lzbench output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_lzbench import parse_lzbench


class LzbenchParserTests(unittest.TestCase):
    def test_single_lz4_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                "Compressor name         Compress. Decompress. Compr. size  Ratio Filename\n"
                f"lz4 1.9.4 970 MB/s 4630 MB/s 5 50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=20.0 dTime=20.0 chunkSize=4KB cSpeed=0MB\n"
            )
            results = parse_lzbench(output, corpus)
            self.assertEqual(results["compression_throughput"]["value"], 970.0)
            self.assertEqual(results["decompression_throughput"]["value"], 4630.0)
            self.assertEqual(results["compressed_size_ratio"]["value"], 50.0)

    def test_rejects_bundled_1_10_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                f"lz4 1.10.0 970 MB/s 4630 MB/s 5 50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=20.0 dTime=20.0 chunkSize=4KB\n"
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                parse_lzbench(output, corpus)


if __name__ == "__main__":
    unittest.main()
