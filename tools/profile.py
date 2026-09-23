"""CI-only, time-windowed profiling of vLLM serve instances."""

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
STOP_TIMEOUT = 900
POLL_INTERVAL = 1


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


def make_instance(name: str, endpoint: str, role: str = "standalone", dp_rank: int = 0) -> ServeInstance:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return ServeInstance(name, endpoint, str(profile_root() / "raw" / safe_name), role, dp_rank)


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
        requests.post(f"{endpoint.rstrip('/')}/start_profile", timeout=30).raise_for_status()

    def stop_profile(self, endpoint: str) -> None:
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
        with ThreadPoolExecutor(max_workers=len(targets) or 1) as pool:
            futures = {pool.submit(getattr(self.client, method), t.endpoint): t.name for t in targets}
            result = {}
            for future in as_completed(futures):
                try:
                    future.result()
                    result[futures[future]] = None
                except Exception as exc:
                    result[futures[future]] = str(exc)
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
            started_at = time.monotonic()
            start = self._parallel("start_profile", self.targets)
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
        self.root, self.output = root, output

    def collect(self, case_name: str, targets: tuple[ServeInstance, ...], result: dict) -> None:
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
                trace_dirs = list(raw_dir.rglob("*_ascend_pt"))
                mode = self.output
                if self.output == "parsed":
                    try:
                        from torch_npu.profiler.profiler import analyse

                        if not trace_dirs:
                            raise ValueError("No Ascend trace directories found")
                        for trace_dir in trace_dirs:
                            analyse(str(trace_dir), max_process_number=1)
                            parsed = trace_dir / "ASCEND_PROFILER_OUTPUT"
                            trace_view = parsed / "trace_view.json"
                            if (
                                not (parsed / "analyse.done").is_file()
                                or not trace_view.is_file()
                                or not trace_view.stat().st_size
                            ):
                                raise ValueError(f"Incomplete parsed trace: {trace_dir}")
                            json.loads(trace_view.read_text())
                    except Exception as exc:
                        state["parse_error"] = str(exc)
                        mode = "raw-fallback"
                with tarfile.open(archive, "w:gz") as tar:
                    if mode == "parsed":
                        for trace_dir in trace_dirs:
                            rel = trace_dir.relative_to(raw_dir)
                            tar.add(
                                trace_dir / "ASCEND_PROFILER_OUTPUT",
                                arcname=str(Path(target.name) / rel / "ASCEND_PROFILER_OUTPUT"),
                            )
                            for metadata in (
                                *trace_dir.glob("profiler_info*.json"),
                                *trace_dir.glob("profiler_metadata.json"),
                            ):
                                tar.add(metadata, arcname=str(Path(target.name) / rel / metadata.name))
                    else:
                        tar.add(raw_dir, arcname=target.name)
                state.update(
                    archive=str(archive.relative_to(self.root)), size_bytes=archive.stat().st_size, output=mode
                )
                # Clear only this target's completed trace, ready for the next case.
                for child in raw_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            except Exception as exc:
                state["artifact_error"] = str(exc)
            records.append({"name": target.name, "endpoint": target.endpoint, **state})
        case_record = {
            "case": case_name,
            "actual_duration": result.get("actual_duration", 0),
            "status": "partial"
            if result.get("status") != "success"
            or any(r.get("parse_error") or r.get("artifact_error") for r in records)
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


def upload_artifacts(root: Path, prefix: str, bucket: str, endpoint: str, region: str) -> dict:
    """Upload target archives in parallel, then publish the manifest last."""
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config

    manifest_path = root / "profile_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    client = boto3.client(
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
    transfer = TransferConfig(
        multipart_threshold=64 * 1024**2,
        multipart_chunksize=64 * 1024**2,
        max_concurrency=4,
        use_threads=True,
    )
    prefix = prefix.strip("/")

    def upload(record: dict) -> None:
        if "archive" not in record:
            return
        source = root / record["archive"]
        key = f"{prefix}/{record['archive']}"
        try:
            client.upload_file(str(source), bucket, key, ExtraArgs={"ContentType": "application/gzip"}, Config=transfer)
            uploaded_size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
            if uploaded_size != source.stat().st_size:
                raise RuntimeError(f"OBS size mismatch: local={source.stat().st_size}, remote={uploaded_size}")
            record["obs_url"] = f"obs://{bucket}/{key}"
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("upload", choices=["upload"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--bucket", default="obs-guiiyang1-ascend-test")
    parser.add_argument("--endpoint", default="https://obs.cn-southwest-2.myhuaweicloud.com")
    parser.add_argument("--region", default="cn-southwest-2")
    args = parser.parse_args()
    upload_artifacts(args.root, args.prefix, args.bucket, args.endpoint, args.region)


if __name__ == "__main__":
    main()
