#!/usr/bin/env python3

import argparse
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

MULTIPART_THRESHOLD_BYTES = 64 * 1024 * 1024
MULTIPART_CHUNK_SIZE_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_CONCURRENCY = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload a profiling archive directly to an S3-compatible OBS bucket.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Profiling archive does not exist: {source}")

    source_size = source.stat().st_size
    if source_size == 0:
        raise ValueError(f"Profiling archive is empty: {source}")

    object_key = args.key.lstrip("/")
    if not object_key:
        raise ValueError("OBS object key must not be empty")

    client = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name=args.region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={
                "addressing_style": "virtual",
                "payload_signing_enabled": False,
            },
        ),
    )
    transfer_config = TransferConfig(
        multipart_threshold=MULTIPART_THRESHOLD_BYTES,
        multipart_chunksize=MULTIPART_CHUNK_SIZE_BYTES,
        max_concurrency=MAX_UPLOAD_CONCURRENCY,
        use_threads=True,
    )

    client.upload_file(
        str(source),
        args.bucket,
        object_key,
        ExtraArgs={"ContentType": "application/gzip"},
        Config=transfer_config,
    )

    uploaded_size = client.head_object(Bucket=args.bucket, Key=object_key)["ContentLength"]
    if uploaded_size != source_size:
        raise RuntimeError(f"OBS object size mismatch: local={source_size}, remote={uploaded_size}")

    print(f"Direct OBS upload verified: obs://{args.bucket}/{object_key} ({uploaded_size} bytes)")


if __name__ == "__main__":
    main()
