"""CI-only, time-windowed profiling of vLLM serve instances."""

import argparse
import json
import logging
import os
import shlex
import shutil
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
STOP_TIMEOUT = 900
POLL_INTERVAL = 1
RANK_PARSE_CONCURRENCY = 1
PUBLISH_CONCURRENCY = 1
PARSED_COMPRESSION_LEVEL = 1


@dataclass(frozen=True)
class ProfileSpec:
    enabled: bool = False
    start_after: int = 15
    duration: int = 8
    with_stack: bool = False
    scope: str = "representative"
    max_size_bytes: int = 50 * 1024**3
    output: str = "parsed"
    cases: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ProfileSpec":
        import regex as re

        enabled = os.getenv("NIGHTLY_PROFILE_ENABLED", "false").lower() == "true"
        if not enabled:
            return cls()
        max_size = os.getenv("NIGHTLY_PROFILE_MAX_SIZE", "50G").upper()
        match = re.fullmatch(r"(\d+)([KMGTP]?)", max_size)
        if not match:
            raise ValueError(f"Invalid profile max size: {max_size}")
        suffix = match.group(2)
        multiplier = 1024 ** ("KMGTP".index(suffix) + 1) if suffix else 1
        spec = cls(
            enabled=True,
            start_after=int(os.getenv("NIGHTLY_PROFILE_START_AFTER", "15")),
            duration=int(os.getenv("NIGHTLY_PROFILE_DURATION", "8")),
            with_stack=os.getenv("NIGHTLY_PROFILE_WITH_STACK", "false").lower() == "true",
            scope=os.getenv("NIGHTLY_PROFILE_SCOPE", "representative"),
            max_size_bytes=int(match.group(1)) * multiplier,
            output=os.getenv("NIGHTLY_PROFILE_OUTPUT", "parsed"),
            cases=tuple(filter(None, os.getenv("NIGHTLY_PROFILE_CASES", "").split(","))),
        )
        if spec.start_after < 0 or spec.duration <= 0 or spec.max_size_bytes <= 0:
            raise ValueError("Profile times and max size must be positive (start-after may be zero)")
        if spec.scope not in ("representative", "all") or spec.output not in ("parsed", "raw"):
            raise ValueError("Invalid profile scope or output mode")
        return spec

    def includes(self, case_name: str, case_type: str) -> bool:
        return self.enabled and case_type == "performance" and (not self.cases or case_name in self.cases)


@dataclass(frozen=True)
class ServeInstance:
    name: str
    endpoint: str
    profile_dir: str
    role: str = "standalone"
    dp_rank: int = 0
    node_index: int = 0


@dataclass(frozen=True)
class ServeManifest:
    instances: tuple[ServeInstance, ...]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "instances": [asdict(i) for i in self.instances]}, indent=2))

    @classmethod
    def read(cls, path: Path) -> "ServeManifest":
        data = json.loads(path.read_text())
        return cls(tuple(ServeInstance(**item) for item in data["instances"]))


class TargetSelector:
    @staticmethod
    def select(manifest: ServeManifest, scope: str) -> tuple[ServeInstance, ...]:
        instances = manifest.instances
        if scope == "all":
            return instances
        roles = {item.role for item in instances}
        if "prefill" in roles or "decode" in roles:
            return tuple(
                min((i for i in instances if i.role == role), key=lambda i: i.dp_rank)
                for role in ("prefill", "decode")
                if role in roles
            )
        return (min(instances, key=lambda i: i.dp_rank),) if instances else ()


def profile_root() -> Path:
    return Path(os.getenv("NIGHTLY_PROFILE_ROOT", "profile_artifact")).resolve()


def make_instance(
    name: str, endpoint: str, role: str = "standalone", dp_rank: int = 0, node_index: int = 0
) -> ServeInstance:
    import regex as re

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return ServeInstance(name, endpoint, str(profile_root() / "raw" / safe_name), role, dp_rank, node_index)


def install_manifest(instances: list[ServeInstance]) -> None:
    root = profile_root()
    ServeManifest(tuple(instances)).write(root / "serve_manifest.json")
    manifest_path = root / "profile_manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps({"requested": asdict(ProfileSpec.from_env()), "cases": [], "status": "skipped"})
        )


def with_profiler_config(command: list[str] | str, instance: ServeInstance, spec: ProfileSpec) -> list[str] | str:
    """Merge only profiler-owned options into the existing server command."""
    args = shlex.split(command) if isinstance(command, str) else list(command)
    flag = "--profiler-config"
    existing = json.loads(args[args.index(flag) + 1]) if flag in args else {}
    existing.update(
        profiler="torch",
        torch_profiler_dir=instance.profile_dir,
        torch_profiler_with_stack=spec.with_stack,
        ignore_frontend=True,
        max_iterations=0,
    )
    if flag in args:
        args[args.index(flag) + 1] = json.dumps(existing)
    else:
        args.extend((flag, json.dumps(existing)))
    return shlex.join(args) if isinstance(command, str) else args


def profiled_model(base_type: type, marker: str) -> type:
    """AISBench model wrapper; marks the first actual request, not process launch."""

    class ProfiledModel(base_type):
        async def stream_infer(self, request_body, output):
            _mark_first_request(marker)
            return await super().stream_infer(request_body, output)

        async def text_infer(self, request_body, output):
            _mark_first_request(marker)
            return await super().text_infer(request_body, output)

    return ProfiledModel


def _mark_first_request(marker: str) -> None:
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as file:
        file.write(str(time.monotonic()))


class ProfileClient:
    def start_profile(self, endpoint: str) -> None:
        import requests

        requests.post(f"{endpoint.rstrip('/')}/start_profile", timeout=30).raise_for_status()

    def stop_profile(self, endpoint: str) -> None:
        import requests

        requests.post(f"{endpoint.rstrip('/')}/stop_profile", timeout=STOP_TIMEOUT).raise_for_status()


def directory_size(path: Path) -> int:
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file()) if path.exists() else 0


class ProfileController:
    """Owns one benchmark case's start/stop lifecycle; never raises into benchmark."""

    def __init__(
        self, spec: ProfileSpec, targets: tuple[ServeInstance, ...], marker: Path, client: ProfileClient | None = None
    ):
        self.spec, self.targets, self.marker = spec, targets, marker
        self.client = client or ProfileClient()
        self.finished = threading.Event()
        self.result: dict = {"status": "skipped", "targets": {}}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> dict:
        self.finished.set()
        self.thread.join(STOP_TIMEOUT + 60)
        return self.result

    def _parallel(self, method: str, targets: tuple[ServeInstance, ...]) -> dict[str, str | None]:
        endpoints: dict[str, list[str]] = {}
        for target in targets:
            endpoints.setdefault(target.endpoint, []).append(target.name)
        with ThreadPoolExecutor(max_workers=len(endpoints) or 1) as pool:
            futures = {
                pool.submit(getattr(self.client, method), endpoint): names for endpoint, names in endpoints.items()
            }
            result = {}
            for future in as_completed(futures):
                try:
                    future.result()
                    error = None
                except Exception as exc:
                    error = str(exc)
                result.update(dict.fromkeys(futures[future], error))
            return result

    def _run(self) -> None:
        try:
            while not self.finished.wait(POLL_INTERVAL):
                if self.marker.exists() and self.marker.stat().st_size:
                    first_request = float(self.marker.read_text())
                    break
            else:
                self.result = {"status": "skipped", "reason": "no_request_before_benchmark_end", "targets": {}}
                return
            if self.finished.wait(max(0, first_request + self.spec.start_after - time.monotonic())):
                self.result = {"status": "skipped", "reason": "benchmark_ended_before_start", "targets": {}}
                return
            print(f"[Profiling] start_profile -> {', '.join(t.name for t in self.targets)}", flush=True)
            start = self._parallel("start_profile", self.targets)
            started_at = time.monotonic()
            active = tuple(t for t in self.targets if start[t.name] is None)
            reason = "duration_reached"
            try:
                while active and not self.finished.wait(POLL_INTERVAL):
                    if time.monotonic() - started_at >= self.spec.duration:
                        break
                    if any(directory_size(Path(t.profile_dir)) >= self.spec.max_size_bytes for t in active):
                        reason = "size_limit_exceeded"
                        break
                if self.finished.is_set() and time.monotonic() - started_at < self.spec.duration:
                    reason = "benchmark_ended"
            finally:
                actual_duration = time.monotonic() - started_at
                print(f"[Profiling] stop_profile ({reason}) -> {', '.join(t.name for t in self.targets)}", flush=True)
                # A failed start response may still have started some workers.
                stop = self._parallel("stop_profile", self.targets)
            self.result = {
                "status": "success"
                if len(active) == len(self.targets)
                and not any(stop.values())
                and reason == "duration_reached"
                and actual_duration >= self.spec.duration
                else "partial",
                "reason": reason,
                "actual_duration": actual_duration,
                "targets": {
                    t.name: {"start_error": start[t.name], "stop_error": stop.get(t.name)} for t in self.targets
                },
            }
        except Exception as exc:
            logger.exception("Profiling controller failed; benchmark result is unchanged")
            self.result = {"status": "partial", "reason": str(exc), "targets": {}}


class ArtifactManager:
    def __init__(self, root: Path, output: str):
        self.root, self.requested_output = root, output

    def collect(self, case_name: str, targets: tuple[ServeInstance, ...], result: dict) -> None:
        import regex as re

        safe_case = re.sub(r"[^A-Za-z0-9_.-]", "_", case_name)
        case_dir = self.root / safe_case
        case_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for target in targets:
            raw_dir = Path(target.profile_dir)
            archive = case_dir / f"{target.name}.tar.gz"
            state = dict(result.get("targets", {}).get(target.name, {}))
            try:
                if not raw_dir.exists() or not any(raw_dir.iterdir()):
                    raise FileNotFoundError(f"No profiler output in {raw_dir}")
                raw_size = directory_size(raw_dir)
                print(f"[Profiling] Compress raw {case_name}/{target.name}: {raw_size} bytes before", flush=True)
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(raw_dir, arcname=target.name)
                state.update(
                    archive=str(archive.relative_to(self.root)), size_bytes=archive.stat().st_size, output="raw"
                )
                print(
                    f"[Profiling] Compressed raw {case_name}/{target.name}: {archive.stat().st_size} bytes after",
                    flush=True,
                )
                # Clear only this target's completed trace, ready for the next case.
                for child in raw_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            except Exception as exc:
                state["artifact_error"] = str(exc)
            records.append({"name": target.name, "endpoint": target.endpoint, "node_index": target.node_index, **state})
        case_record = {
            "case": case_name,
            "actual_duration": result.get("actual_duration", 0),
            "status": "partial"
            if result.get("status") != "success" or any(r.get("artifact_error") for r in records)
            else "success",
            "reason": result.get("reason"),
            "targets": records,
        }
        manifest_path = self.root / "profile_manifest.json"
        manifest = (
            json.loads(manifest_path.read_text())
            if manifest_path.exists()
            else {"requested": asdict(ProfileSpec.from_env()), "cases": []}
        )
        manifest["cases"].append(case_record)
        manifest["status"] = "partial" if any(case["status"] != "success" for case in manifest["cases"]) else "success"
        manifest_path.write_text(json.dumps(manifest, indent=2))


def _obs_client(endpoint: str, region: str):
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


def _transfer_config():
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=64 * 1024**2,
        multipart_chunksize=64 * 1024**2,
        max_concurrency=4,
        use_threads=True,
    )


def _progress(label: str, total: int):
    transferred = 0
    reported = 0
    lock = threading.Lock()

    def callback(amount: int) -> None:
        nonlocal transferred, reported
        with lock:
            transferred += amount
            percent = min(100, transferred * 100 // total)
            step = percent // 10
            if step > reported:
                reported = step
                print(f"[Profiling] {label}: {percent}% ({transferred}/{total} bytes)", flush=True)

    return callback


def _upload_archive(client, transfer, root: Path, prefix: str, bucket: str, record: dict, stage: str) -> None:
    source = root / record["archive"]
    key = f"{prefix}/{record['archive']}"
    size = source.stat().st_size
    print(f"[Profiling] Upload {stage} obs://{bucket}/{key}: {size} bytes", flush=True)
    client.upload_file(
        str(source),
        bucket,
        key,
        ExtraArgs={"ContentType": "application/gzip"},
        Config=transfer,
        Callback=_progress(f"Upload {stage} {record['archive']}", size),
    )
    uploaded_size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if uploaded_size != size:
        raise RuntimeError(f"OBS size mismatch: local={size}, remote={uploaded_size}")
    record["obs_url"] = f"obs://{bucket}/{key}"


def upload_artifacts(root: Path, prefix: str, bucket: str, endpoint: str, region: str, stage: str = "raw") -> dict:
    """Upload archives in parallel and publish the stage manifest last."""
    manifest_path = root / "profile_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    client = _obs_client(endpoint, region)
    transfer = _transfer_config()
    prefix = f"{prefix.strip('/')}/{stage}"

    def upload(record: dict) -> None:
        if "archive" not in record:
            return
        try:
            _upload_archive(client, transfer, root, prefix, bucket, record, stage)
        except Exception as exc:
            record["upload_error"] = str(exc)

    records = [target for case in manifest["cases"] for target in case["targets"]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(upload, records))
    for record in records:
        if record.get("obs_url"):
            print(f"Profiling artifact uploaded: {record['obs_url']}", flush=True)
        elif record.get("upload_error"):
            print(f"Profiling artifact upload failed: {record['archive']}: {record['upload_error']}", flush=True)
    if any(record.get("upload_error") for record in records):
        manifest["status"] = "partial"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    key = f"{prefix}/profile_manifest.json"
    client.upload_file(str(manifest_path), bucket, key, ExtraArgs={"ContentType": "application/json"})
    if client.head_object(Bucket=bucket, Key=key)["ContentLength"] != manifest_path.stat().st_size:
        raise RuntimeError("OBS manifest size mismatch")
    logger.info("Profiling manifest: obs://%s/%s", bucket, key)
    print(f"Profiling manifest uploaded: obs://{bucket}/{key} (status={manifest['status']})", flush=True)
    return manifest


def _download_raw_manifest(root: Path, prefix: str, bucket: str, client, transfer) -> tuple[dict, Path, str]:
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    raw_prefix = f"{prefix.strip('/')}/raw"
    manifest_path = raw_root / "profile_manifest.json"
    print(f"[Profiling] Download raw manifest: obs://{bucket}/{raw_prefix}/profile_manifest.json", flush=True)
    client.download_file(bucket, f"{raw_prefix}/profile_manifest.json", str(manifest_path), Config=transfer)
    return json.loads(manifest_path.read_text()), raw_root, raw_prefix


def _upload_json(client, source: Path, bucket: str, key: str) -> None:
    client.upload_file(str(source), bucket, key, ExtraArgs={"ContentType": "application/json"})
    if client.head_object(Bucket=bucket, Key=key)["ContentLength"] != source.stat().st_size:
        raise RuntimeError(f"OBS size mismatch: {key}")


def plan_artifact_shards(root: Path, prefix: str, bucket: str, endpoint: str, region: str) -> list[int]:
    """Return one parsing shard for every node represented in the raw manifest."""
    client = _obs_client(endpoint, region)
    manifest, _, _ = _download_raw_manifest(root, prefix, bucket, client, _transfer_config())
    node_indexes = sorted(
        {int(target.get("node_index", 0)) for case in manifest.get("cases", []) for target in case.get("targets", [])}
    )
    return node_indexes or [0]


def _prepare_parsed_record(record: dict) -> tuple[str | None, str | None]:
    raw_archive = record.pop("archive", None)
    raw_url = record.pop("obs_url", None)
    record.pop("size_bytes", None)
    record.pop("output", None)
    if raw_url:
        record["raw_obs_url"] = raw_url
    return raw_archive, raw_url


def parse_artifacts(
    root: Path,
    prefix: str,
    bucket: str,
    endpoint: str,
    region: str,
    max_process_number: int = 16,
    node_index: int | None = None,
) -> dict:
    """Parse one node shard while completed ranks are compressed and uploaded."""
    if max_process_number < 1:
        raise ValueError("max_process_number must be positive")

    from botocore.exceptions import ClientError

    client = _obs_client(endpoint, region)
    transfer = _transfer_config()
    try:
        manifest, raw_root, raw_prefix = _download_raw_manifest(root, prefix, bucket, client, transfer)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            print("[Profiling] No raw manifest for this job; skipping offline parse", flush=True)
            return {"status": "skipped", "reason": "raw_manifest_missing"}
        raise

    from torch_npu.profiler.profiler import analyse

    shard_index = 0 if node_index is None else node_index
    parsed_root = root / "parsed"
    parsed_root.mkdir(parents=True, exist_ok=True)
    parsed_prefix = f"{prefix.strip('/')}/parsed"
    upload_client = _obs_client(endpoint, region)
    failures: list[str] = []
    shard_targets = []

    def parse_rank(raw_archive: str, trace_index: int, trace_count: int, trace_dir: Path) -> Path:
        print(
            f"[Profiling] Parse node={shard_index} {raw_archive} trace {trace_index}/{trace_count} "
            f"(rank_concurrency={RANK_PARSE_CONCURRENCY}, max_process_number={max_process_number}): {trace_dir}",
            flush=True,
        )
        started = time.monotonic()
        analyse(str(trace_dir), max_process_number=max_process_number)
        parsed = trace_dir / "ASCEND_PROFILER_OUTPUT"
        trace_view = parsed / "trace_view.json"
        if not (parsed / "analyse.done").is_file() or not trace_view.is_file() or not trace_view.stat().st_size:
            raise ValueError(f"Incomplete parsed trace: {trace_dir}")
        json.loads(trace_view.read_text())
        print(f"[Profiling] Parsed {trace_dir} in {time.monotonic() - started:.1f}s", flush=True)
        return parsed

    def publish_rank(record: dict, rank: dict, trace_dir: Path, relative: Path, parsed: Path, raw_archive: str) -> None:
        archive_path = Path(raw_archive).parent / record["name"] / relative.with_name(f"{relative.name}.tar.gz")
        archive = parsed_root / archive_path
        archive.parent.mkdir(parents=True, exist_ok=True)
        parsed_size = directory_size(parsed) + sum(
            file.stat().st_size
            for pattern in ("profiler_info*.json", "profiler_metadata.json")
            for file in trace_dir.glob(pattern)
        )
        print(f"[Profiling] Compress parsed {archive_path}: {parsed_size} bytes before", flush=True)
        with tarfile.open(archive, "w:gz", compresslevel=PARSED_COMPRESSION_LEVEL) as tar:
            tar.add(parsed, arcname=str(Path(record["name"]) / relative / parsed.name))
            for metadata in (*trace_dir.glob("profiler_info*.json"), *trace_dir.glob("profiler_metadata.json")):
                tar.add(metadata, arcname=str(Path(record["name"]) / relative / metadata.name))
        rank.update(archive=str(archive_path), size_bytes=archive.stat().st_size, output="parsed")
        print(f"[Profiling] Compressed parsed {archive_path}: {archive.stat().st_size} bytes after", flush=True)
        _upload_archive(upload_client, transfer, parsed_root, parsed_prefix, bucket, rank, "parsed")
        print(f"Profiling artifact uploaded: {rank['obs_url']}", flush=True)
        archive.unlink()
        shutil.rmtree(trace_dir)

    for case_index, case in enumerate(manifest.get("cases", [])):
        for target_index, record in enumerate(case.get("targets", [])):
            if node_index is not None and int(record.get("node_index", 0)) != node_index:
                continue
            raw_archive, raw_url = _prepare_parsed_record(record)
            work_dir = root / "work" / f"case_{case_index}_target_{target_index}"
            try:
                if not raw_archive or not raw_url:
                    raise ValueError("Raw archive was not uploaded")
                source = raw_root / raw_archive
                source.parent.mkdir(parents=True, exist_ok=True)
                remote_size = client.head_object(Bucket=bucket, Key=f"{raw_prefix}/{raw_archive}")["ContentLength"]
                print(f"[Profiling] Download raw node={shard_index} {raw_archive}: {remote_size} bytes", flush=True)
                client.download_file(
                    bucket,
                    f"{raw_prefix}/{raw_archive}",
                    str(source),
                    Config=transfer,
                    Callback=_progress(f"Download raw {raw_archive}", remote_size),
                )
                if source.stat().st_size != remote_size:
                    raise RuntimeError(f"OBS download size mismatch: {raw_archive}")
                with tarfile.open(source) as tar:
                    tar.extractall(work_dir)
                source.unlink()

                target_dir = work_dir / record["name"]
                trace_dirs = sorted(target_dir.rglob("*_ascend_pt"))
                if not trace_dirs:
                    raise ValueError(f"No Ascend trace directories in {raw_archive}")
                record["ranks"] = []
                record["output"] = "parsed"
                parse_futures = {}
                publish_futures = {}
                with (
                    ThreadPoolExecutor(max_workers=RANK_PARSE_CONCURRENCY) as parse_pool,
                    ThreadPoolExecutor(max_workers=PUBLISH_CONCURRENCY) as publish_pool,
                ):
                    for trace_index, trace_dir in enumerate(trace_dirs, 1):
                        relative = trace_dir.relative_to(target_dir)
                        rank = {"name": str(relative)}
                        record["ranks"].append(rank)
                        future = parse_pool.submit(parse_rank, raw_archive, trace_index, len(trace_dirs), trace_dir)
                        parse_futures[future] = (rank, trace_dir, relative)
                    for future in as_completed(parse_futures):
                        rank, trace_dir, relative = parse_futures[future]
                        try:
                            parsed = future.result()
                        except Exception as exc:
                            rank["parse_error"] = str(exc)
                            failure = f"{case['case']}/{record['name']}/{relative}: {exc}"
                            failures.append(failure)
                            print(f"[Profiling] Parse failed: {failure}", flush=True)
                            continue
                        published = publish_pool.submit(
                            publish_rank, record, rank, trace_dir, relative, parsed, raw_archive
                        )
                        publish_futures[published] = (rank, relative)
                    for future in as_completed(publish_futures):
                        rank, relative = publish_futures[future]
                        try:
                            future.result()
                        except Exception as exc:
                            rank["upload_error"] = str(exc)
                            failure = f"{case['case']}/{record['name']}/{relative}: {exc}"
                            failures.append(failure)
                            print(f"[Profiling] Publish failed: {failure}", flush=True)
            except Exception as exc:
                record["parse_error"] = str(exc)
                failure = f"{case['case']}/{record['name']}: {exc}"
                failures.append(failure)
                print(f"[Profiling] Parse failed: {failure}", flush=True)
            finally:
                if raw_archive:
                    (raw_root / raw_archive).unlink(missing_ok=True)
                shutil.rmtree(work_dir, ignore_errors=True)
            shard_targets.append({"case_index": case_index, "target_index": target_index, "record": record})

    shard = {
        "node_index": shard_index,
        "status": "partial" if failures else "success",
        "targets": shard_targets,
        "failures": failures,
    }
    shard_path = parsed_root / "shards" / f"node-{shard_index}.json"
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    shard_path.write_text(json.dumps(shard, indent=2))
    shard_key = f"{parsed_prefix}/shards/node-{shard_index}.json"
    _upload_json(client, shard_path, bucket, shard_key)
    print(f"[Profiling] Parsed shard uploaded: obs://{bucket}/{shard_key} (status={shard['status']})", flush=True)

    if node_index is None:
        return finalize_artifacts(root, prefix, bucket, endpoint, region, [shard_index])
    if failures:
        raise RuntimeError(f"Offline profiling node {node_index} failed with {len(failures)} parse/upload failures")
    return shard


def finalize_artifacts(
    root: Path,
    prefix: str,
    bucket: str,
    endpoint: str,
    region: str,
    node_indexes: list[int] | None = None,
) -> dict:
    """Merge node shard manifests and publish the global parsed manifest last."""
    client = _obs_client(endpoint, region)
    transfer = _transfer_config()
    manifest, _, _ = _download_raw_manifest(root, prefix, bucket, client, transfer)
    node_indexes = (
        node_indexes
        or sorted(
            {
                int(target.get("node_index", 0))
                for case in manifest.get("cases", [])
                for target in case.get("targets", [])
            }
        )
        or [0]
    )
    parsed_root = root / "parsed"
    shard_root = parsed_root / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    parsed_prefix = f"{prefix.strip('/')}/parsed"
    parsed_targets = {}
    failures = []

    for shard_index in node_indexes:
        shard_path = shard_root / f"node-{shard_index}.json"
        shard_key = f"{parsed_prefix}/shards/node-{shard_index}.json"
        try:
            client.download_file(bucket, shard_key, str(shard_path), Config=transfer)
            shard = json.loads(shard_path.read_text())
        except Exception as exc:
            failures.append(f"node-{shard_index}: {exc}")
            continue
        failures.extend(shard.get("failures", []))
        for target in shard.get("targets", []):
            parsed_targets[target["case_index"], target["target_index"]] = target["record"]

    for case_index, case in enumerate(manifest.get("cases", [])):
        targets = []
        for target_index, raw_record in enumerate(case.get("targets", [])):
            record = parsed_targets.get((case_index, target_index))
            if record is None:
                record = dict(raw_record)
                _prepare_parsed_record(record)
                record["parse_error"] = "Parsed shard result is missing"
                failures.append(f"{case['case']}/{record['name']}: parsed shard result is missing")
            targets.append(record)
        case["targets"] = targets
        if case.get("status") != "success" or any(
            target.get("parse_error")
            or any(rank.get("parse_error") or rank.get("upload_error") for rank in target.get("ranks", []))
            for target in targets
        ):
            case["status"] = "partial"

    if manifest.get("status") != "success" or failures:
        manifest["status"] = "partial"
    manifest_path = parsed_root / "profile_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    key = f"{parsed_prefix}/profile_manifest.json"
    _upload_json(client, manifest_path, bucket, key)
    print(f"Profiling manifest uploaded: obs://{bucket}/{key} (status={manifest['status']})", flush=True)
    if manifest["status"] == "partial":
        raise RuntimeError(
            f"Offline profiling incomplete: raw/parsed status=partial, {len(failures)} parse/upload failures"
        )
    return manifest


def log_obs_storage(bucket: str, endpoint: str) -> None:
    """Best-effort OBS capacity snapshot; bucket accounting may be delayed."""
    try:
        from obs import ObsClient

        client = ObsClient(
            access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            security_token=os.getenv("AWS_SESSION_TOKEN"),
            server=endpoint,
        )
        try:
            storage = client.getBucketStorageInfo(bucket)
            quota = client.getBucketQuota(bucket)
            if storage.status >= 300 or quota.status >= 300:
                raise RuntimeError(f"storage status={storage.status}, quota status={quota.status}")
            used = int(storage.body.size)
            limit = int(quota.body.quota)
            remaining = f"{max(0, limit - used)} bytes (estimated)" if limit else "unlimited quota"
            print(
                f"[Profiling] OBS capacity {bucket}: used={used} bytes, quota={limit} bytes, remaining={remaining}; "
                "usage is delayed",
                flush=True,
            )
        finally:
            client.close()
    except Exception as exc:
        print(f"[Profiling] OBS capacity unavailable: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["upload", "plan", "parse", "finalize", "storage"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--prefix")
    parser.add_argument("--bucket", default="obs-guiiyang1-ascend-test")
    parser.add_argument("--endpoint", default="https://obs.cn-southwest-2.myhuaweicloud.com")
    parser.add_argument("--region", default="cn-southwest-2")
    parser.add_argument("--max-process-number", type=int, default=16)
    parser.add_argument("--node-index", type=int)
    parser.add_argument("--plan-output", type=Path)
    args = parser.parse_args()
    if args.command == "storage":
        log_obs_storage(args.bucket, args.endpoint)
    else:
        if args.root is None or args.prefix is None:
            parser.error("--root and --prefix are required")
        if args.command == "upload":
            result = upload_artifacts(args.root, args.prefix, args.bucket, args.endpoint, args.region)
            if result["status"] == "partial":
                raise RuntimeError("Profiling raw artifact/upload incomplete; see manifest and per-target errors above")
        elif args.command == "plan":
            node_indexes = plan_artifact_shards(args.root, args.prefix, args.bucket, args.endpoint, args.region)
            plan = json.dumps({"node_index": node_indexes}, separators=(",", ":"))
            if args.plan_output:
                args.plan_output.parent.mkdir(parents=True, exist_ok=True)
                args.plan_output.write_text(plan)
            print(f"[Profiling] Parse shard matrix: {plan}", flush=True)
        elif args.command == "parse":
            parse_artifacts(
                args.root,
                args.prefix,
                args.bucket,
                args.endpoint,
                args.region,
                args.max_process_number,
                args.node_index,
            )
        else:
            finalize_artifacts(args.root, args.prefix, args.bucket, args.endpoint, args.region)


if __name__ == "__main__":
    main()
