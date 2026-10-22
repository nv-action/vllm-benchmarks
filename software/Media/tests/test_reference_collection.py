"""Keep the original shell scripts unchanged and validate the result adapter."""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


MEDIA_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = MEDIA_DIR / "collect_reference.py"
SPEC = importlib.util.spec_from_file_location("collect_reference", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)

ORIGINAL_SHA256 = {
    "x264/scripts/fps-encode-x264.sh": "1f041acad96c7911fa2c3eeacaa983a8bb2ac5fe18df0e0d137d361126eba461",
    "x264/scripts/score-x264.sh": "6255a5cc16ef58ed58f3529ae67e77462ac48ff956e9fc1159aff1abbad06793",
    "x265/scripts/fps-encode-x265.sh": "2ba13537fcca2b9dd1665eca1ce63fc5947cd9c1aea64d5f1fb0278f61732db9",
    "x265/scripts/score-x265.sh": "96205cb6e540e09886e92d34b3688ccf0feecae05ca846ca4e8a21427e03294e",
}


def test_reference_scripts_remain_byte_for_byte_original() -> None:
    for relative_path, expected in ORIGINAL_SHA256.items():
        assert reference.sha256_file(MEDIA_DIR / relative_path) == expected


@pytest.mark.parametrize(
    ("encoder", "log_line"),
    [
        ("x264", "encoded 1 frames, 12.50 fps, 2000.00 kb/s"),
        ("x265", "encoded 1 frames in 0.08s (12.50 fps), 2000.00 kb/s"),
    ],
)
def test_collects_original_script_csv_and_route_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoder: str, log_line: str
) -> None:
    monkeypatch.setattr(reference, "VIDEO_NAMES", ("sample",))
    monkeypatch.setattr(reference, "BITRATES", (2000,))
    monkeypatch.setattr(reference, "FRAME_BYTES", 6)
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    (video_dir / "sample.yuv").write_bytes(b"123456")
    videos = reference.inspect_videos(video_dir)
    route_dir = tmp_path / f"outputfiles-{encoder}" / "2000k" / "sample"
    route_dir.mkdir(parents=True)
    (route_dir / "log_sample_2000k_1.txt").write_text(log_line + "\n", encoding="utf-8")
    (route_dir / "sample_2000k_1.bin").write_bytes(b"1234")
    csv_path = tmp_path / "score.csv"
    csv_path.write_text(
        "Bitrate,Video Directory,Total FPS,Average Bin Size\n"
        "2000k,sample,12.50,4\n",
        encoding="utf-8",
    )
    rows = reference.read_summary(csv_path)
    assert rows[("sample", 2000)] == (Decimal("12.50"), Decimal(4))
    raw_output = tmp_path / "raw.log"
    raw_output.write_text("original command output\n", encoding="utf-8")
    results = reference.collect(encoder, tmp_path, raw_output, rows, videos)
    assert results["sample/2000k/total_fps"]["value"] == 12.5
    assert results["sample/2000k/total_fps"]["group"] == "2000 kb/s"
    assert log_line in raw_output.read_text(encoding="utf-8")


def test_rejects_incomplete_original_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reference, "VIDEO_NAMES", ("sample",))
    monkeypatch.setattr(reference, "BITRATES", (2000,))
    with pytest.raises(ValueError, match="missing"):
        reference.read_summary(tmp_path / "missing.csv")
