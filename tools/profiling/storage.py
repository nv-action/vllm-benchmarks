"""Shared OBS transfer and storage utilities for profiling artifacts."""

import shlex
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_BUCKET = "ascend-profile-artifacts"
DEFAULT_ENDPOINT = "https://obs.cn-southwest-2.myhuaweicloud.com"
DEFAULT_REGION = "cn-southwest-2"
GZIP_COMPRESSION_LEVEL = 1


class ArtifactError(RuntimeError):
    """An artifact validation failure whose message is safe to publish."""


# =============================================================================
# Local and OBS transfer helpers
# =============================================================================


def create_obs_client(endpoint: str, region: str):
    """Create the S3-compatible client used to access Huawei OBS."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "virtual", "payload_signing_enabled": False},
        ),
    )


def create_transfer_config():
    """Return bounded multipart-transfer settings shared by uploads/downloads."""
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=64 * 1024**2,
        multipart_chunksize=64 * 1024**2,
        max_concurrency=4,
        use_threads=True,
    )


def make_transfer_progress_callback(label: str, total_bytes: int):
    """Build a thread-safe callback that reports transfer progress by quarter."""
    transferred = 0
    reported = 0
    lock = threading.Lock()

    def callback(amount: int) -> None:
        """Accumulate one transfer chunk and print newly crossed quarters."""
        nonlocal transferred, reported
        with lock:
            transferred += amount
            percent = min(100, transferred * 100 // total_bytes)
            step = percent // 25
            if step > reported:
                reported = step
                print(f"[Profiling] {label}: {percent}% ({transferred}/{total_bytes} bytes)", flush=True)

    return callback


def upload_archive(
    client,
    transfer_config,
    root: Path,
    prefix: str,
    bucket: str,
    target_record: dict,
    stage: str,
) -> None:
    """Upload one archive and record its location after the SDK reports success."""
    archive_path = root / target_record["archive"]
    object_key = f"{prefix}/{target_record['archive']}"
    size_bytes = archive_path.stat().st_size
    print(f"[Profiling] Upload {stage} obs://{bucket}/{object_key}: {size_bytes} bytes", flush=True)
    client.upload_file(
        str(archive_path),
        bucket,
        object_key,
        ExtraArgs={"ContentType": "application/gzip"},
        Config=transfer_config,
        Callback=make_transfer_progress_callback(f"Upload {stage} {target_record['archive']}", size_bytes),
    )
    target_record["obs_url"] = f"obs://{bucket}/{object_key}"


def upload_json(client, source_path: Path, bucket: str, object_key: str) -> None:
    """Upload a JSON artifact, propagating SDK failures."""
    client.upload_file(str(source_path), bucket, object_key, ExtraArgs={"ContentType": "application/json"})


# =============================================================================
# Download summary
# =============================================================================


def obs_http_url(obs_url: str, endpoint: str) -> str:
    """Convert an OBS object URI to an unsigned, virtual-hosted HTTP(S) URL."""
    bucket, key = obs_url.removeprefix("obs://").split("/", 1)
    server = urlsplit(endpoint)
    return f"{server.scheme}://{bucket}.{server.netloc}/{quote(key, safe='/')}"


def print_download_summary(
    manifest: dict, bucket: str, prefix: str, endpoint: str, stage: str, manifest_uploaded: bool
) -> None:
    """List confirmed uploads and ready-to-copy batch download commands.

    Parsed finalization includes the raw links carried forward in target
    records. Public objects can be downloaded through unsigned HTTPS links.
    """
    links = []
    for case in manifest.get("cases", []):
        for target in case.get("targets", []):
            label = f"{case['case']}/{target['name']}"
            raw_url = target.get("raw_obs_url") if stage == "parsed" else target.get("obs_url")
            if raw_url:
                links.append(("raw", label, raw_url))
            for rank in target.get("ranks", []):
                if rank.get("obs_url"):
                    links.append(("parsed", f"{label}/{rank['name']}", rank["obs_url"]))
    prefix = prefix.strip("/")
    if stage == "parsed":
        # Finalization downloaded this manifest successfully before merging.
        links.append(("raw", "manifest", f"obs://{bucket}/{prefix}/raw/profile_manifest.json"))
    if manifest_uploaded:
        links.append((stage, "manifest", f"obs://{bucket}/{prefix}/{stage}/profile_manifest.json"))
    print(f"\n{'=' * 60}\nProfiling downloads (status={manifest['status']})\n{'=' * 60}", flush=True)
    for output in ("raw", "parsed"):
        output_links = [(label, url) for kind, label, url in links if kind == output]
        if not output_links:
            continue
        print(f"[{output}]", flush=True)
        for label, url in output_links:
            print(f"{label}\n{obs_http_url(url, endpoint)}", flush=True)
        source = shlex.quote(f"obs://{bucket}/{prefix}/{output}/")
        print(f"obsutil cp {source} ./profile-download/{output}/ -r -j=4", flush=True)
    if not links:
        print("No confirmed uploads available.", flush=True)
    print(
        "Unsigned links need no credentials for public objects; recursive obsutil downloads also require list access.\n"
        + "=" * 60,
        flush=True,
    )
