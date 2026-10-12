"""Exercise current profiling OBS settings with isolated, disposable objects."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from tools.profiling.storage import (
    DEFAULT_BUCKET,
    DEFAULT_ENDPOINT,
    DEFAULT_REGION,
    create_obs_client,
    create_transfer_config,
    obs_http_url,
    upload_archive,
    upload_json,
)


def report_failure(stage: str, exc: Exception) -> None:
    """Log only structured error metadata, including wrapped transfer errors."""
    details = {"stage": stage, "exception": type(exc).__name__}
    cause = exc
    while cause is not None:
        response = getattr(cause, "response", None)
        if isinstance(response, dict):
            details.update(
                operation=getattr(cause, "operation_name", "unknown"),
                http_status=response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
                error_code=response.get("Error", {}).get("Code"),
            )
            break
        if isinstance(cause, HTTPError):
            details["http_status"] = cause.code
            break
        cause = cause.__cause__ or cause.__context__
    print(f"::error::OBS smoke failure: {json.dumps(details, ensure_ascii=True)}", flush=True)


@contextmanager
def check(stage: str):
    """Expose the failing operation without dumping SDK messages or requests."""
    print(f"[OBS smoke] START: {stage}", flush=True)
    try:
        yield
    except Exception as exc:
        report_failure(stage, exc)
        raise
    report(stage)


def report(stage: str) -> None:
    print(f"[OBS smoke] PASS: {stage}", flush=True)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"- PASS: {stage}\n")


def main() -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise RuntimeError(f"Required secret mapping is empty: {name}")
    prefix = (
        f"nightly-profiling/{os.environ['GITHUB_REPOSITORY']}/"
        f"{os.environ['GITHUB_RUN_ID']}/{os.environ['GITHUB_RUN_ATTEMPT']}/obs-smoke"
    )
    print(f"[OBS smoke] bucket={DEFAULT_BUCKET}, region={DEFAULT_REGION}, prefix={prefix}", flush=True)
    client = create_obs_client(DEFAULT_ENDPOINT, DEFAULT_REGION)
    keys = [f"{prefix}/probe.tar.gz", f"{prefix}/profile_manifest.json"]
    failures = []
    with tempfile.TemporaryDirectory(prefix="obs-smoke-") as directory:
        root = Path(directory)
        payload = b"profiling OBS smoke test\n" * 4096
        with tarfile.open(root / "probe.tar.gz", "w:gz") as archive:
            entry = tarfile.TarInfo("probe.txt")
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        record = {"archive": "probe.tar.gz"}
        manifest = root / "profile_manifest.json"
        manifest.write_text(json.dumps({"kind": "obs-smoke", "run_id": os.environ["GITHUB_RUN_ID"]}))
        try:
            with check("archive upload and remote size verification"):
                upload_archive(client, create_transfer_config(), root, prefix, DEFAULT_BUCKET, record, "smoke")
            with check("JSON upload and remote size verification"):
                upload_json(client, manifest, DEFAULT_BUCKET, keys[1])
            with check("list uploaded objects"):
                listed = client.list_objects_v2(Bucket=DEFAULT_BUCKET, Prefix=f"{prefix}/")
                if not set(keys).issubset({item["Key"] for item in listed.get("Contents", [])}):
                    raise RuntimeError("Uploaded objects missing from prefix listing")
            for key in keys:
                expected = hashlib.sha256((root / key.rsplit("/", 1)[-1]).read_bytes()).digest()
                destination = root / "download"
                with check(f"signed download and SHA256 verification: {Path(key).name}"):
                    client.download_file(DEFAULT_BUCKET, key, str(destination), Config=create_transfer_config())
                    if hashlib.sha256(destination.read_bytes()).digest() != expected:
                        raise RuntimeError("Signed download checksum mismatch")
                with check(f"anonymous download and SHA256 verification: {Path(key).name}"):
                    url = obs_http_url(f"obs://{DEFAULT_BUCKET}/{key}", DEFAULT_ENDPOINT)
                    with urlopen(url, timeout=60) as response:
                        if hashlib.sha256(response.read()).digest() != expected:
                            raise RuntimeError("Public download checksum mismatch")
        except Exception as exc:
            failures.append(f"transfer verification: {type(exc).__name__}")
        finally:
            for key in keys:
                try:
                    with check(f"delete test object: {Path(key).name}"):
                        client.delete_object(Bucket=DEFAULT_BUCKET, Key=key)
                except Exception as exc:
                    failures.append(f"cleanup: {type(exc).__name__}")
            if not any(item.startswith("cleanup:") for item in failures):
                with check("list prefix to verify cleanup"):
                    remaining = client.list_objects_v2(Bucket=DEFAULT_BUCKET, Prefix=f"{prefix}/")
                    if any(item["Key"] in keys for item in remaining.get("Contents", [])):
                        raise RuntimeError("Test objects still present")
    if failures:
        raise RuntimeError("; ".join(failures))
    report("current profiling OBS configuration is usable")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        report_failure("overall verification", exc)
        raise SystemExit(1) from None
