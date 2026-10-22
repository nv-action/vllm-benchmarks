"""Contract tests for the Zstd level-5 lzbench output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_lzbench import parse_lzbench


class LzbenchParserTests(unittest.TestCase):
    def test_single_zstd_level_five_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                "Compressor name         Compress. Decompress. Compr. size  Ratio Filename\n"
                f"zstd 1.5.7 -5 100 MB/s 728 MB/s 5 50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=20.0 dTime=20.0 chunkSize=4KB cSpeed=0MB\n"
            )
            results = parse_lzbench(output, corpus)
            self.assertEqual(results["compression_throughput"]["value"], 100.0)
            self.assertEqual(results["decompression_throughput"]["value"], 728.0)
            self.assertEqual(results["compressed_size_ratio"]["value"], 50.0)

    def test_rejects_other_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "silesia.tar"
            corpus.write_bytes(b"0123456789")
            output = (
                f"zstd 1.5.7 -3 100 MB/s 728 MB/s 5 50.00 {corpus}\n"
                "[Params] cIters=1 dIters=1 cTime=20.0 dTime=20.0 chunkSize=4KB\n"
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                parse_lzbench(output, corpus)


if __name__ == "__main__":
    unittest.main()
