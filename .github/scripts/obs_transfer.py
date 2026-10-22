#!/usr/bin/env python3
"""Transfer complete performance-result directories through Huawei OBS.

Credentials and endpoint settings are read only from environment variables:
OBS_ENDPOINT, OBS_BUCKET, OBS_PREFIX, OBS_AK, OBS_SK, and optionally
OBS_SECURITY_TOKEN.  PERF_PROXY optionally configures an explicit SDK proxy.
A manifest is uploaded last so report jobs only consume complete directory
uploads and can verify every downloaded file.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


MANIFEST_NAME = ".boostkit-obs-manifest.json"
COMPRESSED_MANIFEST_NAME = f"{MANIFEST_NAME}.gz"
TRANSFER_CHUNK_SIZE = 64 * 1024


def fail(message: str) -> None:
    raise RuntimeError(message)


def obs_settings() -> tuple[str, str, str, str, str, str | None]:
    values = {name: os.environ.get(name, "").strip() for name in (
        "OBS_ENDPOINT", "OBS_BUCKET", "OBS_PREFIX", "OBS_AK", "OBS_SK"
    )}
    missing = [name for name, value in values.items() if not value]
    if missing:
        fail("missing required OBS settings: " + ", ".join(missing))
    endpoint = values["OBS_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        fail("OBS_ENDPOINT must include http:// or https://")
    prefix = values["OBS_PREFIX"].strip("/")
    if not prefix:
        fail("OBS_PREFIX must not be empty")
    token = os.environ.get("OBS_SECURITY_TOKEN", "").strip() or None
    return (
        endpoint,
        values["OBS_BUCKET"],
        prefix,
        values["OBS_AK"],
        values["OBS_SK"],
        token,
    )


def proxy_settings() -> dict[str, Any]:
    proxy_url = os.environ.get("PERF_PROXY", "").strip()
    if not proxy_url:
        return {}

    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        fail("PERF_PROXY must be http[s]://host:port")
    try:
        proxy_port = parsed.port
    except ValueError as error:
        fail(f"PERF_PROXY has an invalid port: {error}")
    if proxy_port is None:
        fail("PERF_PROXY must include a port")

    settings: dict[str, Any] = {
        "proxy_host": parsed.hostname,
        "proxy_port": proxy_port,
    }
    if parsed.username:
        settings["proxy_username"] = unquote(parsed.username)
    if parsed.password:
        settings["proxy_password"] = unquote(parsed.password)
    return settings


def client() -> tuple[Any, str, str]:
    try:
        from obs import ObsClient
    except ImportError as error:
        fail(f"OBS SDK import failed: {error}")

    endpoint, bucket, prefix, access_key, secret_key, security_token = obs_settings()
    kwargs: dict[str, Any] = {
        "access_key_id": access_key,
        "secret_access_key": secret_key,
        "server": endpoint,
    }
    if security_token:
        kwargs["security_token"] = security_token
    kwargs.update(proxy_settings())
    return ObsClient(**kwargs), bucket, prefix


def response_ok(response: Any, action: str, object_key: str) -> None:
    status = getattr(response, "status", None)
    if not isinstance(status, int) or not 200 <= status < 300:
        error_code = getattr(response, "errorCode", "unknown")
        error_message = getattr(response, "errorMessage", "unknown error")
        fail(f"OBS {action} failed for {object_key}: {status} {error_code} {error_message}")


def relative_file_path(root: Path, file_path: Path) -> str:
    relative = file_path.relative_to(root)
    if file_path.is_symlink():
        fail(f"refusing to transfer symbolic link: {relative}")
    return relative.as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_key(prefix: str, *parts: str) -> str:
    safe_parts: list[str] = []
    for part in parts:
        value = part.strip("/")
        if not value or value in {".", ".."} or ".." in PurePosixPath(value).parts:
            fail(f"unsafe OBS path component: {part!r}")
        safe_parts.append(value)
    return "/".join((prefix, *safe_parts))


def upload_file(
    obs_client: Any,
    bucket: str,
    key: str,
    path: Path,
    relative: str,
) -> list[dict[str, Any]]:
    if path.stat().st_size <= TRANSFER_CHUNK_SIZE:
        response = obs_client.putFile(bucket, key, str(path))
        response_ok(response, "upload", key)
        return []

    chunks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="boostkit-obs-part-") as temporary_directory:
        chunk_path = Path(temporary_directory) / "chunk"
        with path.open("rb") as source:
            part_number = 0
            while content := source.read(TRANSFER_CHUNK_SIZE):
                part_number += 1
                relative_part = f"{relative}.parts/{part_number:06d}"
                part_key = f"{key}.parts/{part_number:06d}"
                chunk_path.write_bytes(content)
                response = obs_client.putFile(bucket, part_key, str(chunk_path))
                response_ok(response, "upload", part_key)
                chunks.append({
                    "path": relative_part,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
    return chunks


def download_file(obs_client: Any, bucket: str, key: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    response = obs_client.getObject(bucket, key, str(path))
    response_ok(response, "download", key)


def download_chunks(
    obs_client: Any,
    bucket: str,
    base_key: str,
    chunks: list[dict[str, Any]],
    target: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_target = target.with_name(f".{target.name}.partial")
    with tempfile.TemporaryDirectory(prefix="boostkit-obs-part-") as temporary_directory:
        chunk_path = Path(temporary_directory) / "chunk"
        with temporary_target.open("wb") as output:
            for chunk in chunks:
                relative = chunk.get("path")
                expected_size = chunk.get("size")
                expected_sha256 = chunk.get("sha256")
                if (
                    not isinstance(relative, str)
                    or not isinstance(expected_size, int)
                    or not isinstance(expected_sha256, str)
                ):
                    fail("invalid OBS file chunk in manifest")
                safe_relative = PurePosixPath(relative)
                if safe_relative.is_absolute() or ".." in safe_relative.parts:
                    fail(f"unsafe OBS file chunk path in manifest: {relative!r}")
                key = f"{base_key}/{relative}"
                download_file(obs_client, bucket, key, chunk_path)
                if chunk_path.stat().st_size != expected_size or sha256(chunk_path) != expected_sha256:
                    fail(f"checksum mismatch after OBS download: {relative}")
                with chunk_path.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
    temporary_target.replace(target)


def upload_manifest(obs_client: Any, bucket: str, base_key: str, manifest: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="boostkit-obs-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        manifest_path = temporary_path / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        root_manifest: dict[str, Any] = manifest

        if manifest_path.stat().st_size > TRANSFER_CHUNK_SIZE:
            compressed_path = temporary_path / COMPRESSED_MANIFEST_NAME
            compressed_path.write_bytes(gzip.compress(manifest_path.read_bytes()))
            chunks = upload_file(
                obs_client,
                bucket,
                f"{base_key}/{COMPRESSED_MANIFEST_NAME}",
                compressed_path,
                COMPRESSED_MANIFEST_NAME,
            )
            root_manifest = {
                "schema": 2,
                "compressed_manifest": {
                    "path": COMPRESSED_MANIFEST_NAME,
                    "size": compressed_path.stat().st_size,
                    "sha256": sha256(compressed_path),
                    "chunks": chunks,
                },
            }
            manifest_path.write_text(
                json.dumps(root_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        if manifest_path.stat().st_size > TRANSFER_CHUNK_SIZE:
            fail("OBS manifest index exceeds the proxy upload limit")
        manifest_key = f"{base_key}/{MANIFEST_NAME}"
        response = obs_client.putFile(bucket, manifest_key, str(manifest_path))
        response_ok(response, "upload", manifest_key)


def compressed_manifest_details(manifest: dict[str, Any]) -> dict[str, Any] | None:
    details = manifest.get("compressed_manifest")
    if details is None:
        return None
    if not isinstance(details, dict):
        fail("invalid compressed OBS manifest index")
    path = details.get("path")
    size = details.get("size")
    checksum = details.get("sha256")
    chunks = details.get("chunks")
    if (
        path != COMPRESSED_MANIFEST_NAME
        or not isinstance(size, int)
        or not isinstance(checksum, str)
        or not isinstance(chunks, list)
    ):
        fail("invalid compressed OBS manifest details")
    return details


def upload_directory(args: argparse.Namespace) -> None:
    source = Path(args.source).resolve()
    if not source.is_dir():
        fail(f"result directory does not exist: {source}")
    obs_client, bucket, prefix = client()
    if args.kind == "result":
        base_key = object_key(
            prefix,
            args.category,
            args.software,
            args.version,
            args.result_directory,
            args.architecture,
        )
    else:
        base_key = object_key(prefix, "reports", args.result_directory)
    files = [path for path in sorted(source.rglob("*")) if path.is_file()]
    if not files:
        fail(f"result directory contains no files: {source}")

    manifest_files = []
    for path in files:
        relative = relative_file_path(source, path)
        manifest_entry: dict[str, Any] = {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        key = f"{base_key}/{relative}"
        print(f"[obs] uploading {relative}", flush=True)
        chunks = upload_file(obs_client, bucket, key, path, relative)
        if chunks:
            manifest_entry["chunks"] = chunks
        manifest_files.append(manifest_entry)

    manifest: dict[str, Any] = {
        "schema": 2,
        "kind": args.kind,
        "run_id": args.run_id,
        "result_directory": args.result_directory,
        "files": manifest_files,
    }
    if args.kind == "result":
        manifest.update({
            "category": args.category,
            "software": args.software,
            "version": args.version,
            "architecture": args.architecture,
        })
    upload_manifest(obs_client, bucket, base_key, manifest)
    print(f"[obs] uploaded {len(manifest_files)} files to obs://{bucket}/{base_key}", flush=True)


def load_manifest(obs_client: Any, bucket: str, key: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="boostkit-obs-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        manifest_path = temporary_path / MANIFEST_NAME
        download_file(obs_client, bucket, key, manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            fail(f"invalid OBS manifest {key}: {error}")
        details = compressed_manifest_details(manifest) if isinstance(manifest, dict) else None
        if details is not None:
            compressed_path = temporary_path / COMPRESSED_MANIFEST_NAME
            chunks = details["chunks"]
            if chunks:
                download_chunks(obs_client, bucket, str(Path(key).parent), chunks, compressed_path)
            else:
                download_file(obs_client, bucket, f"{Path(key).parent}/{COMPRESSED_MANIFEST_NAME}", compressed_path)
            if compressed_path.stat().st_size != details["size"] or sha256(compressed_path) != details["sha256"]:
                fail(f"checksum mismatch after OBS download: {COMPRESSED_MANIFEST_NAME}")
            try:
                manifest = json.loads(gzip.decompress(compressed_path.read_bytes()))
            except (OSError, json.JSONDecodeError) as error:
                fail(f"invalid compressed OBS manifest {key}: {error}")
    if not isinstance(manifest, dict):
        fail(f"invalid OBS manifest object: {key}")
    return manifest


def manifest_value(manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value or "/" in value or value in {".", ".."}:
        fail(f"invalid {field} in OBS manifest")
    return value


def matrix_entries(matrix_path: Path) -> list[dict[str, str]]:
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid execution matrix: {error}")
    entries = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(entries, list) or not entries:
        fail("execution matrix has no include entries")
    identities: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail("execution matrix contains an invalid entry")
        identity = {
            field: manifest_value(entry, field)
            for field in ("category", "software", "version", "arch")
        }
        key = tuple(identity.values())
        if key not in seen:
            seen.add(key)
            identities.append(identity)
    return identities


def download_results(args: argparse.Namespace) -> None:
    destination = Path(args.destination).resolve()
    obs_client, bucket, prefix = client()
    downloaded = 0
    for identity in matrix_entries(Path(args.matrix)):
        category = identity["category"]
        software = identity["software"]
        version = identity["version"]
        architecture = identity["arch"]
        base_key = object_key(
            prefix, category, software, version, args.result_directory, architecture
        )
        manifest_key = f"{base_key}/{MANIFEST_NAME}"
        manifest = load_manifest(obs_client, bucket, manifest_key)
        if manifest.get("kind") != "result" or manifest.get("run_id") != args.run_id:
            fail(f"OBS manifest run ID does not match requested run: {manifest_key}")
        manifest_identity = {
            "category": manifest_value(manifest, "category"),
            "software": manifest_value(manifest, "software"),
            "version": manifest_value(manifest, "version"),
            "arch": manifest_value(manifest, "architecture"),
        }
        if manifest_identity != identity or manifest.get("result_directory") != args.result_directory:
            fail(f"OBS manifest identity does not match the execution matrix: {manifest_key}")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            fail(f"OBS manifest has no files: {manifest_key}")
        base_path = destination / category / software / version / architecture
        for entry in files:
            if not isinstance(entry, dict):
                fail(f"invalid file entry in OBS manifest: {manifest_key}")
            relative = entry.get("path")
            expected_size = entry.get("size")
            expected_sha256 = entry.get("sha256")
            if (
                not isinstance(relative, str)
                or not isinstance(expected_size, int)
                or not isinstance(expected_sha256, str)
            ):
                fail(f"invalid file details in OBS manifest: {manifest_key}")
            safe_relative = PurePosixPath(relative)
            if safe_relative.is_absolute() or ".." in safe_relative.parts or relative in {"", "."}:
                fail(f"unsafe file path in OBS manifest: {relative!r}")
            target = base_path.joinpath(*safe_relative.parts)
            print(f"[obs] downloading {category}/{software}/{version}/{architecture}/{relative}", flush=True)
            chunks = entry.get("chunks")
            if chunks is None:
                download_file(obs_client, bucket, f"{base_key}/{relative}", target)
            elif isinstance(chunks, list) and chunks:
                for chunk in chunks:
                    if not isinstance(chunk, dict):
                        fail(f"invalid OBS file chunks in manifest: {relative}")
                    chunk_path = chunk.get("path")
                    if not isinstance(chunk_path, str) or not chunk_path.startswith(f"{relative}.parts/"):
                        fail(f"invalid OBS file chunk path in manifest: {relative}")
                download_chunks(obs_client, bucket, base_key, chunks, target)
            else:
                fail(f"invalid OBS file chunks in manifest: {relative}")
            if target.stat().st_size != expected_size:
                fail(f"size mismatch after OBS download: {relative}")
            if sha256(target) != expected_sha256:
                fail(f"checksum mismatch after OBS download: {relative}")
            downloaded += 1
    if not downloaded:
        fail(f"no result files were downloaded for run {args.run_id}")
    print(f"[obs] downloaded {downloaded} result files to {destination}", flush=True)


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = argument_parser.add_subparsers(dest="command", required=True)
    upload = subparsers.add_parser("upload-result", help="upload one complete architecture result")
    upload.add_argument("--source", required=True)
    upload.add_argument("--run-id", required=True)
    upload.add_argument("--category", required=True)
    upload.add_argument("--software", required=True)
    upload.add_argument("--version", required=True)
    upload.add_argument("--architecture", required=True)
    upload.add_argument("--result-directory", required=True)
    upload.set_defaults(kind="result")
    upload.set_defaults(handler=upload_directory)
    upload_report = subparsers.add_parser("upload-report", help="upload the consolidated report")
    upload_report.add_argument("--source", required=True)
    upload_report.add_argument("--run-id", required=True)
    upload_report.add_argument("--result-directory", required=True)
    upload_report.set_defaults(kind="report", handler=upload_directory)
    download = subparsers.add_parser("download-results", help="download all results in the execution matrix")
    download.add_argument("--destination", required=True)
    download.add_argument("--run-id", required=True)
    download.add_argument("--result-directory", required=True)
    download.add_argument("--matrix", required=True)
    download.set_defaults(handler=download_results)
    return argument_parser


def main() -> int:
    try:
        args = parser().parse_args()
        args.handler(args)
    except RuntimeError as error:
        print(f"[obs] ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
