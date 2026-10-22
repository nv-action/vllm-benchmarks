"""Validate the eight upstream rdma_performance TCP scenarios."""

import pytest

from parse_benchmark import ATTACHMENT_SIZES, REPETITIONS, client_workload, parse_client_log
from standalone_runtime import extract_metrics


def make_log() -> str:
    lines = []
    for size in ATTACHMENT_SIZES:
        threads, depth = client_workload(size)
        for _ in range(REPETITIONS):
            lines.append(f"[Threads: {threads}, Depth: {depth}, Attachment: {size}B, RDMA: no, Echo: yes]")
            lines.append(
                "Avg-Latency: 51, 90th-Latency: 90, 99th-Latency: 120, "
                "99.9th-Latency: 145, Throughput: 43.5MB/s, "
                f"QPS: {'0' if size == 8388608 else '44'}k, "
                "Server CPU-utilization: 20%, Client CPU-utilization: 25%"
            )
    return "\n".join(lines) + "\n"


def test_parses_eight_sizes_and_repetitions() -> None:
    results = parse_client_log(make_log())
    assert len(results) == len(ATTACHMENT_SIZES) * 6
    assert results["attachment_0B_qps"]["value"] == 44000
    assert results["attachment_1024B_qps"]["value"] == 43.5 * 1024
    assert results["attachment_8388608B_qps"]["value"] == 43.5 / 8
    assert results["attachment_8388608B_qps"]["source_field"] == "client_stdout.Throughput"
    assert results["attachment_1024B_p999_latency"]["samples"] == [145.0] * REPETITIONS
    assert results["attachment_102400B_qps"]["queue_depth"] == 10
    assert results["attachment_8388608B_qps"]["client_threads"] == 4


def test_client_workload_caps_inflight_attachment_bytes() -> None:
    expected = ((32, 32), (32, 32), (32, 32), (32, 32),
                (32, 10), (32, 5), (32, 1), (4, 1))
    assert tuple(client_workload(size) for size in ATTACHMENT_SIZES) == expected


def test_standalone_result_uses_all_scenario_groups() -> None:
    benchmark = {
        "software": "brpc",
        "version": "1.17.0",
        "architecture": "x86_64",
        "results": parse_client_log(make_log()),
    }
    metrics = extract_metrics(benchmark, "1.17.0", "x86_64")
    assert len(metrics) == len(ATTACHMENT_SIZES) * 6
    assert metrics["attachment_8388608B_qps"]["group"] == "8388608B attachment"


def test_rejects_wrong_scenario() -> None:
    with pytest.raises(ValueError, match="no preceding TCP echo scenario"):
        parse_client_log(make_log().replace("RDMA: no", "RDMA: yes", 1))


def test_rejects_wrong_concurrency() -> None:
    with pytest.raises(ValueError, match="102400B requires 32 threads and depth 10"):
        parse_client_log(make_log().replace("Depth: 10, Attachment: 102400B", "Depth: 32, Attachment: 102400B", 1))


def test_rejects_failed_rpc() -> None:
    with pytest.raises(ValueError, match="failed RPC"):
        parse_client_log(make_log() + "RPC call failed: E1008\n")


def test_rejects_missing_summary() -> None:
    with pytest.raises(ValueError, match="requires 5 client summaries"):
        parse_client_log(make_log().split("Avg-Latency:")[0])


def test_rejects_missing_size() -> None:
    with pytest.raises(ValueError, match="requires 5 client summaries"):
        parse_client_log(make_log().split("[Threads: 4, Depth: 1, Attachment: 8388608B")[0])
