"""Parse raw profiling artifacts and publish parsed results from offline jobs."""

import argparse
import json
import shutil
import tarfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.profiling.storage import (
    DEFAULT_BUCKET,
    DEFAULT_ENDPOINT,
    DEFAULT_REGION,
    GZIP_COMPRESSION_LEVEL,
    ArtifactError,
    create_obs_client,
    create_transfer_config,
    directory_size_bytes,
    make_transfer_progress_callback,
    print_download_summary,
    upload_archive,
    upload_json,
)

# =============================================================================
# Raw artifact discovery and shard planning
# =============================================================================

RANK_PARSE_CONCURRENCY = 1
PUBLISH_CONCURRENCY = 1
DEFAULT_ANALYSIS_PROCESS_COUNT = 16


def _download_raw_manifest(root: Path, prefix: str, bucket: str, client, transfer_config) -> tuple[dict, Path, str]:
    """Download and decode the raw manifest, returning its local and remote roots."""
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    raw_prefix = f"{prefix.strip('/')}/raw"
    manifest_path = raw_root / "profile_manifest.json"
    print(f"[Profiling] Download raw manifest: obs://{bucket}/{raw_prefix}/profile_manifest.json", flush=True)
    client.download_file(
        bucket,
        f"{raw_prefix}/profile_manifest.json",
        str(manifest_path),
        Config=transfer_config,
    )
    return json.loads(manifest_path.read_text()), raw_root, raw_prefix


def plan_artifact_shards(root: Path, prefix: str, bucket: str, endpoint: str, region: str) -> list[int]:
    """Return one parsing shard for every node represented in the raw manifest."""
    download_client = create_obs_client(endpoint, region)
    raw_manifest, _, _ = _download_raw_manifest(root, prefix, bucket, download_client, create_transfer_config())
    node_indexes = sorted(
        {
            target_record["node_index"]
            for case_record in raw_manifest["cases"]
            for target_record in case_record["targets"]
        }
    )
    if not node_indexes and raw_manifest["status"] != "skipped":
        raise ArtifactError("Profiling manifest has no targets but is not skipped")
    return node_indexes


# =============================================================================
# Rank parsing and publication
# =============================================================================


@dataclass(frozen=True)
class _ParseContext:
    """Shared paths, clients, and settings for one node's parsing work."""

    root: Path
    raw_root: Path
    raw_prefix: str
    parsed_root: Path
    parsed_prefix: str
    bucket: str
    node_index: int
    max_process_number: int
    analyse: Callable
    download_client: Any
    upload_client: Any
    transfer_config: Any


def _to_parsed_target_record(raw_target_record: dict) -> tuple[dict, str | None, str | None]:
    """Copy a raw target record and replace its archive fields for parsed output."""
    parsed_target_record = dict(raw_target_record)
    raw_archive = parsed_target_record.pop("archive", None)
    raw_obs_url = parsed_target_record.pop("obs_url", None)
    parsed_target_record.pop("size_bytes", None)
    parsed_target_record.pop("output", None)
    if raw_obs_url:
        parsed_target_record["raw_obs_url"] = raw_obs_url
    return parsed_target_record, raw_archive, raw_obs_url


def _analyse_rank_trace(
    context: _ParseContext,
    *,
    raw_archive: str,
    trace_index: int,
    trace_count: int,
    trace_dir: Path,
) -> Path:
    """Run torch-npu analysis for one rank and validate its parsed trace."""
    print(
        f"[Profiling] Parse node={context.node_index} {raw_archive} trace {trace_index}/{trace_count} "
        f"(rank_concurrency={RANK_PARSE_CONCURRENCY}, max_process_number={context.max_process_number}): {trace_dir}",
        flush=True,
    )
    started_at = time.monotonic()
    context.analyse(str(trace_dir), max_process_number=context.max_process_number)
    parsed_trace_dir = trace_dir / "ASCEND_PROFILER_OUTPUT"
    trace_view = parsed_trace_dir / "trace_view.json"
    if not (parsed_trace_dir / "analyse.done").is_file() or not trace_view.is_file() or not trace_view.stat().st_size:
        raise ArtifactError(f"Incomplete parsed trace: {trace_dir}")
    print(f"[Profiling] Parsed {trace_dir} in {time.monotonic() - started_at:.1f}s", flush=True)
    return parsed_trace_dir


def _publish_parsed_rank(
    context: _ParseContext,
    *,
    target_name: str,
    rank_record: dict,
    trace_dir: Path,
    rank_relative_path: Path,
    parsed_trace_dir: Path,
    raw_archive: str,
) -> None:
    """Compress and upload one parsed rank, then release its local workspace."""
    archive_relative_path = (
        Path(raw_archive).parent / target_name / rank_relative_path.with_name(f"{rank_relative_path.name}.tar.gz")
    )
    archive_path = context.parsed_root / archive_relative_path
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_size_bytes = directory_size_bytes(parsed_trace_dir) + sum(
        file.stat().st_size
        for pattern in ("profiler_info*.json", "profiler_metadata.json")
        for file in trace_dir.glob(pattern)
    )
    print(f"[Profiling] Compress parsed {archive_relative_path}: {parsed_size_bytes} bytes before", flush=True)
    with tarfile.open(archive_path, "w:gz", compresslevel=GZIP_COMPRESSION_LEVEL) as tar:
        tar.add(parsed_trace_dir, arcname=str(Path(target_name) / rank_relative_path / parsed_trace_dir.name))
        for metadata in (*trace_dir.glob("profiler_info*.json"), *trace_dir.glob("profiler_metadata.json")):
            tar.add(metadata, arcname=str(Path(target_name) / rank_relative_path / metadata.name))
    rank_record.update(
        archive=str(archive_relative_path),
        size_bytes=archive_path.stat().st_size,
        output="parsed",
    )
    print(
        f"[Profiling] Compressed parsed {archive_relative_path}: {archive_path.stat().st_size} bytes after",
        flush=True,
    )
    upload_archive(
        context.upload_client,
        context.transfer_config,
        context.parsed_root,
        context.parsed_prefix,
        context.bucket,
        rank_record,
        "parsed",
    )
    print(f"Profiling artifact uploaded: {rank_record['obs_url']}", flush=True)
    archive_path.unlink()
    shutil.rmtree(trace_dir)


def _parse_target_ranks(
    context: _ParseContext,
    *,
    case_name: str,
    raw_archive: str,
    target_record: dict,
    target_dir: Path,
) -> None:
    """Parse and publish all ranks from one extracted target archive."""
    trace_dirs = sorted(target_dir.rglob("*_ascend_pt"))
    if not trace_dirs:
        raise ArtifactError(f"No Ascend trace directories in {raw_archive}")

    target_record["ranks"] = []
    target_record["output"] = "parsed"
    parse_futures = {}
    publish_futures = {}
    with (
        ThreadPoolExecutor(max_workers=RANK_PARSE_CONCURRENCY) as parse_pool,
        ThreadPoolExecutor(max_workers=PUBLISH_CONCURRENCY) as publish_pool,
    ):
        for trace_index, trace_dir in enumerate(trace_dirs, 1):
            rank_relative_path = trace_dir.relative_to(target_dir)
            rank_record = {"name": str(rank_relative_path)}
            target_record["ranks"].append(rank_record)
            parse_future = parse_pool.submit(
                _analyse_rank_trace,
                context,
                raw_archive=raw_archive,
                trace_index=trace_index,
                trace_count=len(trace_dirs),
                trace_dir=trace_dir,
            )
            parse_futures[parse_future] = (rank_record, trace_dir, rank_relative_path)

        for parse_future in as_completed(parse_futures):
            rank_record, trace_dir, rank_relative_path = parse_futures[parse_future]
            try:
                parsed_trace_dir = parse_future.result()
            except Exception as exc:
                rank_record["parse_error"] = str(exc) if isinstance(exc, ArtifactError) else type(exc).__name__
                failure_message = (
                    f"{case_name}/{target_record['name']}/{rank_relative_path}: {rank_record['parse_error']}"
                )
                print(f"[Profiling] Parse failed: {failure_message}", flush=True)
                continue
            publish_future = publish_pool.submit(
                _publish_parsed_rank,
                context,
                target_name=target_record["name"],
                rank_record=rank_record,
                trace_dir=trace_dir,
                rank_relative_path=rank_relative_path,
                parsed_trace_dir=parsed_trace_dir,
                raw_archive=raw_archive,
            )
            publish_futures[publish_future] = (rank_record, rank_relative_path)

        for publish_future in as_completed(publish_futures):
            rank_record, rank_relative_path = publish_futures[publish_future]
            try:
                publish_future.result()
            except Exception as exc:
                rank_record["upload_error"] = str(exc) if isinstance(exc, ArtifactError) else type(exc).__name__
                failure_message = (
                    f"{case_name}/{target_record['name']}/{rank_relative_path}: {rank_record['upload_error']}"
                )
                print(f"[Profiling] Publish failed: {failure_message}", flush=True)


def _parse_target_artifact(
    context: _ParseContext,
    *,
    case_name: str,
    case_index: int,
    target_index: int,
    raw_target_record: dict,
) -> dict:
    """Download, extract, parse, and publish one target's raw artifact."""
    target_record, raw_archive, raw_obs_url = _to_parsed_target_record(raw_target_record)
    work_dir = context.root / "work" / f"case_{case_index}_target_{target_index}"
    try:
        if not raw_archive or not raw_obs_url:
            raise ArtifactError("Raw archive was not uploaded")
        raw_archive_path = context.raw_root / raw_archive
        raw_archive_path.parent.mkdir(parents=True, exist_ok=True)
        object_key = f"{context.raw_prefix}/{raw_archive}"
        remote_size_bytes = context.download_client.head_object(Bucket=context.bucket, Key=object_key)["ContentLength"]
        print(
            f"[Profiling] Download raw node={context.node_index} {raw_archive}: {remote_size_bytes} bytes", flush=True
        )
        context.download_client.download_file(
            context.bucket,
            object_key,
            str(raw_archive_path),
            Config=context.transfer_config,
            Callback=make_transfer_progress_callback(f"Download raw {raw_archive}", remote_size_bytes),
        )
        if raw_archive_path.stat().st_size != remote_size_bytes:
            raise ArtifactError(f"OBS download size mismatch: {raw_archive}")
        with tarfile.open(raw_archive_path) as tar:
            tar.extractall(work_dir)
        raw_archive_path.unlink()
        _parse_target_ranks(
            context,
            case_name=case_name,
            raw_archive=raw_archive,
            target_record=target_record,
            target_dir=work_dir / target_record["name"],
        )
    except Exception as exc:
        target_record["parse_error"] = str(exc) if isinstance(exc, ArtifactError) else type(exc).__name__
        failure_message = f"{case_name}/{target_record['name']}: {target_record['parse_error']}"
        print(f"[Profiling] Parse failed: {failure_message}", flush=True)
    finally:
        if raw_archive:
            (context.raw_root / raw_archive).unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)
    return target_record


def _target_failures(target: dict) -> list[str]:
    """Derive parse/upload failures from target and rank records, once each."""
    failures = []
    for record in [target, *target.get("ranks", [])]:
        label = target["name"] if record is target else f"{target['name']}/{record['name']}"
        for field in ("parse_error", "upload_error"):
            if record.get(field):
                failures.append(f"{label}: {field}: {record[field]}")
    return failures


def parse_artifact_shard(
    root: Path,
    prefix: str,
    bucket: str,
    endpoint: str,
    region: str,
    node_index: int,
    max_process_number: int = DEFAULT_ANALYSIS_PROCESS_COUNT,
) -> dict:
    """Parse and publish the target artifacts assigned to one node."""
    if max_process_number < 1:
        raise ValueError("max_process_number must be positive")

    download_client = create_obs_client(endpoint, region)
    transfer_config = create_transfer_config()
    raw_manifest, raw_root, raw_prefix = _download_raw_manifest(root, prefix, bucket, download_client, transfer_config)
    if raw_manifest["status"] == "skipped" and not raw_manifest["cases"]:
        return {"status": "skipped", "targets": [], "failures": []}

    from torch_npu.profiler.profiler import analyse

    parsed_root = root / "parsed"
    parsed_root.mkdir(parents=True, exist_ok=True)
    parsed_prefix = f"{prefix.strip('/')}/parsed"
    context = _ParseContext(
        root=root,
        raw_root=raw_root,
        raw_prefix=raw_prefix,
        parsed_root=parsed_root,
        parsed_prefix=parsed_prefix,
        bucket=bucket,
        node_index=node_index,
        max_process_number=max_process_number,
        analyse=analyse,
        download_client=download_client,
        upload_client=create_obs_client(endpoint, region),
        transfer_config=transfer_config,
    )
    shard_target_records = []

    for case_index, case_record in enumerate(raw_manifest["cases"]):
        for target_index, raw_target_record in enumerate(case_record["targets"]):
            if raw_target_record["node_index"] != node_index:
                continue
            target_record = _parse_target_artifact(
                context,
                case_name=case_record["case"],
                case_index=case_index,
                target_index=target_index,
                raw_target_record=raw_target_record,
            )
            shard_target_records.append(
                {"case_index": case_index, "target_index": target_index, "record": target_record}
            )

    failures = [
        f"{raw_manifest['cases'][target['case_index']]['case']}/{failure}"
        for target in shard_target_records
        for failure in _target_failures(target["record"])
    ]
    shard = {
        "node_index": node_index,
        "status": "partial" if failures else "success",
        "targets": shard_target_records,
        "failures": failures,
    }
    shard_path = parsed_root / "shards" / f"node-{node_index}.json"
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    shard_path.write_text(json.dumps(shard, indent=2))
    shard_key = f"{parsed_prefix}/shards/node-{node_index}.json"
    upload_json(download_client, shard_path, bucket, shard_key)
    print(f"[Profiling] Parsed shard uploaded: obs://{bucket}/{shard_key} (status={shard['status']})", flush=True)
    return shard


# =============================================================================
# Parsed manifest finalization
# =============================================================================


def _merge_parsed_shards(raw_manifest: dict, shards: list[dict]) -> tuple[dict, list[str]]:
    """Merge shard target records into a copied raw manifest."""
    parsed_manifest = deepcopy(raw_manifest)
    if raw_manifest["status"] == "skipped" and not raw_manifest["cases"]:
        return parsed_manifest, []
    parsed_records_by_position = {}
    failures: list[str] = []
    for shard in shards:
        for shard_target in shard["targets"]:
            position = shard_target["case_index"], shard_target["target_index"]
            parsed_records_by_position[position] = shard_target["record"]

    for case_index, case_record in enumerate(parsed_manifest["cases"]):
        target_records = []
        case_failures = []
        for target_index, raw_target_record in enumerate(case_record["targets"]):
            target_record = parsed_records_by_position.get((case_index, target_index))
            if target_record is None:
                target_record, _, _ = _to_parsed_target_record(raw_target_record)
                target_record["parse_error"] = "Parsed shard result is missing"
            else:
                target_record = deepcopy(target_record)
            case_failures.extend(f"{case_record['case']}/{failure}" for failure in _target_failures(target_record))
            target_records.append(target_record)
        case_record["targets"] = target_records
        failures.extend(case_failures)
        if case_record.get("status") != "success" or case_failures:
            case_record["status"] = "partial"

    if parsed_manifest.get("status") != "success" or failures:
        parsed_manifest["status"] = "partial"
    return parsed_manifest, failures


def finalize_parsed_manifest(
    root: Path,
    prefix: str,
    bucket: str,
    endpoint: str,
    region: str,
    node_indexes: list[int] | None = None,
) -> dict:
    """Merge node shard manifests and publish the global parsed manifest last."""
    client = create_obs_client(endpoint, region)
    transfer_config = create_transfer_config()
    raw_manifest, _, _ = _download_raw_manifest(root, prefix, bucket, client, transfer_config)
    if raw_manifest["status"] == "skipped" and not raw_manifest["cases"]:
        print("[Profiling] No cases selected; skipping finalization", flush=True)
        return raw_manifest
    node_indexes = node_indexes or sorted(
        {
            target_record["node_index"]
            for case_record in raw_manifest["cases"]
            for target_record in case_record["targets"]
        }
    )
    parsed_root = root / "parsed"
    shard_root = parsed_root / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    parsed_prefix = f"{prefix.strip('/')}/parsed"
    shards = []
    missing_shards = []

    for shard_index in node_indexes:
        shard_path = shard_root / f"node-{shard_index}.json"
        shard_key = f"{parsed_prefix}/shards/node-{shard_index}.json"
        try:
            client.download_file(bucket, shard_key, str(shard_path), Config=transfer_config)
            shards.append(json.loads(shard_path.read_text()))
        except Exception as exc:
            missing_shards.append(shard_index)
            print(f"[Profiling] Read shard node-{shard_index} failed: {type(exc).__name__}", flush=True)

    parsed_manifest, failures = _merge_parsed_shards(raw_manifest, shards)
    if missing_shards:
        parsed_manifest["status"] = "partial"
    manifest_path = parsed_root / "profile_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(parsed_manifest, indent=2))
    manifest_key = f"{parsed_prefix}/profile_manifest.json"
    manifest_uploaded = False
    try:
        upload_json(client, manifest_path, bucket, manifest_key)
        manifest_uploaded = True
        print(
            f"Profiling manifest uploaded: obs://{bucket}/{manifest_key} (status={parsed_manifest['status']})",
            flush=True,
        )
    finally:
        print_download_summary(parsed_manifest, bucket, prefix, endpoint, "parsed", manifest_uploaded)
    if parsed_manifest["status"] == "partial":
        raise ArtifactError(
            f"Offline profiling incomplete: {len(failures)} parse/upload failures, "
            f"{len(missing_shards)} missing shards; see manifest for raw/parsed status"
        )
    return parsed_manifest


# =============================================================================
# Command-line entry point
# =============================================================================


def main() -> None:
    """Plan, execute, or finalize offline parsing work."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "parse", "finalize"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-process-number", type=int, default=DEFAULT_ANALYSIS_PROCESS_COUNT)
    parser.add_argument("--node-index", type=int)
    parser.add_argument("--plan-output", type=Path)
    args = parser.parse_args()
    if args.command == "parse" and args.node_index is None:
        parser.error("parse requires --node-index")
    if args.command == "plan":
        node_indexes = plan_artifact_shards(args.root, args.prefix, args.bucket, args.endpoint, args.region)
        plan = json.dumps({"node_index": node_indexes}, separators=(",", ":"))
        if args.plan_output:
            args.plan_output.parent.mkdir(parents=True, exist_ok=True)
            args.plan_output.write_text(plan)
        print(f"[Profiling] Parse shard matrix: {plan}", flush=True)
    elif args.command == "parse":
        shard = parse_artifact_shard(
            args.root,
            args.prefix,
            args.bucket,
            args.endpoint,
            args.region,
            args.node_index,
            args.max_process_number,
        )
        if shard["status"] == "partial":
            raise ArtifactError(
                f"Offline profiling node {args.node_index}: {len(shard['failures'])} parse/upload failures"
            )
    else:
        finalize_parsed_manifest(args.root, args.prefix, args.bucket, args.endpoint, args.region)


if __name__ == "__main__":
    from boto3.exceptions import S3UploadFailedError
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        main()
    except ArtifactError as exc:
        raise SystemExit(f"[Profiling] {exc}") from None
    except (BotoCoreError, ClientError, S3UploadFailedError) as exc:
        raise SystemExit(f"[Profiling] OBS operation failed: {type(exc).__name__}") from None
