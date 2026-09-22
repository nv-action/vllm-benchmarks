"""The formal AISBench request is the shared timing origin."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.profiling import capture


@pytest.fixture
def inferencer_class(monkeypatch):
    class GenInferencer:
        async def warmup(self, requests):
            return await asyncio.gather(*(self.do_request(request, None, None) for request in requests))

        async def do_request(self, data, token_bucket, session):
            if data == "failed":
                raise RuntimeError("request failed")
            return data

    name = "ais_bench.benchmark.openicl.icl_inferencer"
    dependency = ModuleType(name)
    dependency.GenInferencer = GenInferencer
    monkeypatch.setitem(sys.modules, name, dependency)
    spec = importlib.util.spec_from_file_location(
        "profiling_request_marker_under_test", Path(capture.__file__).with_name("aisbench_request_marker.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TimedGenInferencer


@pytest.mark.parametrize("warmup_requests", [[], ["warmup", "warmup"]])
def test_first_formal_request_sets_origin_once(tmp_path, monkeypatch, inferencer_class, warmup_requests):
    marker = tmp_path / "first_request"
    monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(marker))
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "true")
    now = Mock(return_value=50.0)
    monkeypatch.setattr(capture, "time", SimpleNamespace(perf_counter=now))
    inferencer = inferencer_class()
    assert asyncio.run(inferencer.warmup(warmup_requests)) == warmup_requests
    assert not marker.exists()
    now.return_value = 100.0
    assert asyncio.run(inferencer.do_request("first", None, None)) == "first"
    now.return_value = 200.0
    assert asyncio.run(inferencer.do_request("next", None, None)) == "next"
    assert asyncio.run(inferencer_class().do_request("another worker", None, None)) == "another worker"
    assert float(marker.read_text()) == 100.0


def test_failed_request_still_defines_benchmark_origin(tmp_path, monkeypatch, inferencer_class):
    marker = tmp_path / "first_request"
    monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(marker))
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "true")
    now = Mock(return_value=100.0)
    monkeypatch.setattr(capture, "time", SimpleNamespace(perf_counter=now))
    inferencer = inferencer_class()
    with pytest.raises(RuntimeError, match="request failed"):
        asyncio.run(inferencer.do_request("failed", None, None))
    now.return_value = 200.0
    asyncio.run(inferencer.do_request("successful", None, None))
    assert float(marker.read_text()) == 100.0


@pytest.mark.parametrize("profiling", [False, True])
def test_marker_failure_is_fatal_only_for_profiling(tmp_path, monkeypatch, inferencer_class, caplog, profiling):
    monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(tmp_path / "missing-directory/first_request"))
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", str(profiling))
    inferencer = inferencer_class()
    if profiling:
        with pytest.raises(OSError):
            asyncio.run(inferencer.do_request("first", None, None))
    else:
        assert asyncio.run(inferencer.do_request("first", None, None)) == "first"
        assert asyncio.run(inferencer.do_request("second", None, None)) == "second"
        assert len(caplog.records) == 1
        assert "time origin unavailable" in caplog.text
