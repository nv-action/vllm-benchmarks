"""Validate x264 bitstreams and independently decoded output."""
import json
import sys
from pathlib import Path


def validate_decode(probe, decoded_size):
    streams = probe.get("streams", [])
    if len(streams) != 1:
        raise ValueError("expected exactly one video stream")
    stream = streams[0]
    if (stream.get("codec_name"), stream.get("width"), stream.get("height")) != (
        "h264", 64, 64
    ):
        raise ValueError("expected 64x64 H.264")
    if str(stream.get("nb_read_frames")) != "3":
        raise ValueError("expected exactly 3 decoded frames")
    if decoded_size != 64 * 64 * 3 // 2 * 3:
        raise ValueError("decoded I420 size differs from 18432 bytes")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "decode":
            validate_decode(json.loads(Path(sys.argv[2]).read_text()),
                            Path(sys.argv[3]).stat().st_size)
        else:
            raise ValueError("unknown validation mode")
    except (ValueError, OSError) as error:
        print(f"validation failed: {error}", file=sys.stderr)
        sys.exit(1)
