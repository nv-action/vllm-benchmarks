"""Check the three single-instance Redis throughput scenarios."""

import json
import subprocess
from pathlib import Path

import pytest

import benchmark_redis as benchmark_module
from benchmark_redis import CLIENT_COUNTS, benchmark_command, parse_throughput_results
from standalone_runtime import extract_metrics, render_report


def sample_output() -> str:
    return "".join(
        f"# clients={clients}\n"
        f"$ redis-benchmark -c {clients} -t set,get -q\n"
        f"SET: {clients * 1000}.50 requests per second, p50=0.143 msec\r\n"
        f"GET: {clients * 1200}.25 requests per second, p50=0.120 msec\r\n"
        for clients in CLIENT_COUNTS
    )


def test_commands_use_the_requested_workload() -> None:
    command = benchmark_command(Path("/opt/redis-benchmark"), 30000, 40)
    assert command == [
        "/opt/redis-benchmark", "-h", "127.0.0.1", "-p", "30000",
        "-c", "40", "-d", "3", "-n", "10000000", "-r", "10000000",
        "-t", "set,get", "--threads", "20", "-q",
    ]


def test_parses_only_six_throughput_metrics() -> None:
    results = parse_throughput_results(sample_output())
    assert len(results) == 6
    assert results["clients_40_set"]["value"] == 40000.5
    assert results["clients_80_get"]["value"] == 96000.25
    assert results["clients_80_get"]["group"] == "80 clients"
    assert results["clients_80_get"]["source_field"] == "GET: <value> requests per second"


def test_standalone_metrics_keep_clients_and_operations_separate() -> None:
    benchmark = {
        "benchmark": "redis_single_instance_set_get_throughput",
        "software": "redis",
        "version": "7.0.15",
        "architecture": "x86_64",
        "results": parse_throughput_results(sample_output()),
    }
    metrics = extract_metrics(benchmark, "7.0.15", "x86_64")
    assert len(metrics) == 6
    assert metrics["40 clients SET: requests per second"]["group"] == "40 clients"

    report = render_report({
        "version": "7.0.15",
        "run_id": "test-run",
        "architecture": "x86_64",
        "status": "passed",
        "cleanup_status": "passed",
        "build_info": {},
        "system_info": {},
        "metrics": metrics,
    })
    assert report.count("### 40 并发") == 1
    assert report.count("### 60 并发") == 1
    assert report.count("### 80 并发") == 1
    assert "| SET | 40000.5 | 越大越好 |" in report
    assert "| GET | 96000.25 | 越大越好 |" in report


def test_rejects_missing_or_duplicate_operations() -> None:
    with pytest.raises(RuntimeError, match="exactly one SET and one GET"):
        parse_throughput_results(sample_output().replace("GET: 48000.25", "GEX: 48000.25"))
    with pytest.raises(RuntimeError, match="exactly one SET and one GET"):
        parse_throughput_results(sample_output().replace("GET: 48000.25", "SET: 48000.25"))


def test_rejects_missing_client_run_and_zero_throughput() -> None:
    with pytest.raises(RuntimeError, match="40, 60, 80 client runs"):
        parse_throughput_results(sample_output().replace("# clients=60", "# clients=100"))
    with pytest.raises(RuntimeError, match="invalid throughput"):
        parse_throughput_results(sample_output().replace("SET: 40000.50", "SET: 0"))


def test_main_resets_only_its_private_instance_and_saves_six_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    benchmark = tmp_path / "redis-benchmark"
    redis_cli = tmp_path / "redis-cli"
    for binary in (benchmark, redis_cli):
        binary.touch(mode=0o755)
        binary.chmod(0o755)
    raw_output = tmp_path / "raw.log"
    normalized_output = tmp_path / "benchmark.json"
    flush_commands = []

    def fake_flush(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        flush_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "OK\n", "")

    def fake_benchmark(
        binary: Path, port: int, clients: int, raw: Path, deadline: float
    ) -> None:
        assert binary == benchmark and port == 30000 and deadline > 0
        with raw.open("a", encoding="utf-8") as stream:
            stream.write(
                f"# clients={clients}\n"
                f"SET: {clients * 1000}.50 requests per second\n"
                f"GET: {clients * 1200}.25 requests per second\n"
            )

    monkeypatch.setattr(benchmark_module.subprocess, "run", fake_flush)
    monkeypatch.setattr(benchmark_module, "run_benchmark", fake_benchmark)
    monkeypatch.setattr(benchmark_module.sys, "argv", [
        "benchmark_redis.py", str(benchmark), str(raw_output), str(normalized_output)
    ])
    monkeypatch.setenv("REDIS_SERVICE_PORT", "30000")
    monkeypatch.setenv("SOFTWARE_VERSION", "7.0.15")
    monkeypatch.setenv("EXPECTED_ARCH", "x86_64")

    assert benchmark_module.main() == 0
    assert flush_commands == [
        [str(redis_cli), "-h", "127.0.0.1", "-p", "30000", "FLUSHALL"]
        for _ in CLIENT_COUNTS
    ]
    payload = json.loads(normalized_output.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 6
    assert payload["parameters"]["client_counts"] == [40, 60, 80]
