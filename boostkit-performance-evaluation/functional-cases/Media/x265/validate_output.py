"""Validate x265 quality summaries and independently decoded output."""
import json
import math
import re
import sys
from pathlib import Path


def validate_metrics(text):
    summaries = re.findall(
        r"encoded\s+(\d+)\s+frames[^\r\n]*Global PSNR:\s*([^,\s]+)"
        r"[^\r\n]*SSIM Mean Y:\s*([^\s,]+)", text
    )
    if not summaries:
        raise ValueError("missing final PSNR/SSIM summary")
    frames, psnr, ssim = summaries[-1]
    psnr, ssim = float(psnr), float(ssim)
    if int(frames) != 3:
        raise ValueError("expected exactly 3 encoded frames")
    if not math.isfinite(psnr) or psnr <= 0:
        raise ValueError("PSNR must be finite and positive")
    if not math.isfinite(ssim) or not 0 <= ssim <= 1:
        raise ValueError("SSIM must be finite and within [0, 1]")


def validate_decode(probe, decoded_size):
    streams = probe.get("streams", [])
    if len(streams) != 1:
        raise ValueError("expected exactly one video stream")
    stream = streams[0]
    if (stream.get("codec_name"), stream.get("width"), stream.get("height")) != (
        "hevc", 64, 64
    ):
        raise ValueError("expected 64x64 HEVC")
    if str(stream.get("nb_read_frames")) != "3":
        raise ValueError("expected exactly 3 decoded frames")
    if decoded_size != 64 * 64 * 3 // 2 * 3:
        raise ValueError("decoded I420 size differs from 18432 bytes")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "metrics":
            validate_metrics(Path(sys.argv[2]).read_text())
        elif sys.argv[1] == "decode":
            validate_decode(json.loads(Path(sys.argv[2]).read_text()),
                            Path(sys.argv[3]).stat().st_size)
        else:
            raise ValueError("unknown validation mode")
    except (ValueError, OSError) as error:
        print(f"validation failed: {error}", file=sys.stderr)
        sys.exit(1)
