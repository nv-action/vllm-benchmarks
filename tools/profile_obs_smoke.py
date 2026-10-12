"""Exercise current profiling OBS settings with isolated, disposable objects."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
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
            upload_archive(client, create_transfer_config(), root, prefix, DEFAULT_BUCKET, record, "smoke")
            upload_json(client, manifest, DEFAULT_BUCKET, keys[1])
            report("archive and JSON upload; remote sizes match")
            listed = client.list_objects_v2(Bucket=DEFAULT_BUCKET, Prefix=f"{prefix}/")
            if not set(keys).issubset({item["Key"] for item in listed.get("Contents", [])}):
                raise RuntimeError("Uploaded objects missing from prefix listing")
            report("list objects under this run's prefix")
            for key in keys:
                expected = hashlib.sha256((root / key.rsplit("/", 1)[-1]).read_bytes()).digest()
                destination = root / "download"
                client.download_file(DEFAULT_BUCKET, key, str(destination), Config=create_transfer_config())
                if hashlib.sha256(destination.read_bytes()).digest() != expected:
                    raise RuntimeError("Signed download checksum mismatch")
                url = obs_http_url(f"obs://{DEFAULT_BUCKET}/{key}", DEFAULT_ENDPOINT)
                with urlopen(url, timeout=60) as response:
                    if hashlib.sha256(response.read()).digest() != expected:
                        raise RuntimeError("Public download checksum mismatch")
            report("signed and anonymous HTTPS downloads; SHA256 matches for both objects")
        except Exception as exc:
            failures.append(f"transfer verification: {type(exc).__name__}")
        finally:
            for key in keys:
                try:
                    client.delete_object(Bucket=DEFAULT_BUCKET, Key=key)
                except Exception as exc:
                    failures.append(f"cleanup: {type(exc).__name__}")
            if not any(item.startswith("cleanup:") for item in failures):
                remaining = client.list_objects_v2(Bucket=DEFAULT_BUCKET, Prefix=f"{prefix}/")
                if any(item["Key"] in keys for item in remaining.get("Contents", [])):
                    failures.append("cleanup: test objects still present")
                else:
                    report("deleted only the two test objects; prefix listing confirms cleanup")
    if failures:
        raise RuntimeError("; ".join(failures))
    report("current profiling OBS configuration is usable")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # SDK exception messages can include infrastructure and request details.
        print(f"::error::OBS smoke test failed ({type(exc).__name__})", flush=True)
        raise SystemExit(1) from None
