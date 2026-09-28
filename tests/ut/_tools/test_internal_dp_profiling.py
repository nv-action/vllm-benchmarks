import json
from types import SimpleNamespace

from tests.e2e.common.multi_node.internal_dp.profiling import configure_profiling
from tools.profile import ProfileSpec, ServeManifest


def test_headless_internal_dp_is_collected_in_all_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTLY_PROFILE_ROOT", str(tmp_path))
    leader = SimpleNamespace(index=0, ip="leader", headless=False, envs={"SERVER_PORT": 8080})
    worker = SimpleNamespace(index=1, ip="worker", headless=True, envs={"SERVER_PORT": 8080})
    config = SimpleNamespace(
        nodes=[leader, worker],
        cur_node=leader,
        server_port=8080,
        server_cmd=["--tensor-parallel-size", "8"],
        disagg_cfg=None,
        is_master=True,
    )
    spec = ProfileSpec(enabled=True, scope="all")
    configure_profiling(config, spec)
    instances = ServeManifest.read(tmp_path / "serve_manifest.json").instances
    assert [instance.name for instance in instances] == ["dp-0", "dp-1"]
    assert {instance.endpoint for instance in instances} == {"http://leader:8080"}
    assert len({instance.profile_dir for instance in instances}) == 2
    leader_profiler = json.loads(config.server_cmd[config.server_cmd.index("--profiler-config") + 1])
    assert leader_profiler["torch_profiler_dir"] == instances[0].profile_dir

    config.cur_node = worker
    config.is_master = False
    config.server_cmd = ["--headless"]
    configure_profiling(config, spec)
    worker_profiler = json.loads(config.server_cmd[config.server_cmd.index("--profiler-config") + 1])
    assert worker_profiler["torch_profiler_dir"] == instances[1].profile_dir


def test_headless_internal_dp_keeps_representative_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTLY_PROFILE_ROOT", str(tmp_path))
    leader = SimpleNamespace(index=0, ip="leader", headless=False, envs={})
    worker = SimpleNamespace(index=1, ip="worker", headless=True, envs={})
    config = SimpleNamespace(
        nodes=[leader, worker],
        cur_node=worker,
        server_port=8000,
        server_cmd=["--headless"],
        disagg_cfg=None,
        is_master=False,
    )
    configure_profiling(config, ProfileSpec(enabled=True, scope="representative"))
    assert config.server_cmd == ["--headless"]
