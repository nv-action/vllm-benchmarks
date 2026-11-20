# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("build_e2e_csrc.sh")


@pytest.fixture
def build_tree(tmp_path):
    root = tmp_path / "source"
    (root / "vllm_ascend").mkdir(parents=True)
    l0 = tmp_path / "l0"
    l0.mkdir()
    (l0 / "cached.so").write_text("L0 library")
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "candidate").write_text("OBS entry")
    (seed / "existing").write_text("older OBS entry")
    local = tmp_path / "local"
    local.mkdir()
    (local / "existing").write_text("newer local entry")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    uv = binary_dir / "uv"
    # Replace the expensive installer while exercising real restore/merge logic.
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "compile = os.environ['COMPILE_CUSTOM_KERNELS']\n"
        "cache = Path(os.environ['VLLM_ASCEND_BUILD_CACHE_DIR']) if compile == '1' else None\n"
        "Path('observed.json').write_text(json.dumps({'compile': compile,\n"
        "    'cache': sorted(p.name for p in cache.iterdir()) if cache else [],\n"
        "    'cached_library': Path('vllm_ascend/cached.so').exists()}))\n"
        "if os.environ.get('TEST_BUILD_FAIL') == '1': raise SystemExit(17)\n"
        "if cache:\n"
        "    (cache / 'new-entry').write_text('compiled')\n"
        "    Path('vllm_ascend/new.so').write_text('new')\n"
    )
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)}
    return root, l0, seed, local, env


def run_build(build_tree, mode, hit):
    root, l0, seed, local, env = build_tree
    result = subprocess.run(
        ["bash", str(SCRIPT.resolve()), mode, hit, str(l0), str(seed), str(local)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, json.loads((root / "observed.json").read_text())


@pytest.mark.parametrize("hit", ["true", "false"])
def test_disabled_cache_ignores_l0_and_existing_l1(build_tree, hit):
    result, observed = run_build(build_tree, "off", hit)
    assert result.returncode == 0, result.stderr
    assert observed == {"compile": "1", "cache": [], "cached_library": False}
    local = build_tree[3]
    assert list(local.iterdir()) == [local / "existing"]
    assert (build_tree[1] / "cached.so").is_file()
    assert (build_tree[2] / "candidate").is_file()


def test_l0_hit_installs_without_native_compilation(build_tree):
    result, observed = run_build(build_tree, "on", "true")
    assert result.returncode == 0, result.stderr
    assert observed == {"compile": "0", "cache": [], "cached_library": True}
    assert not (build_tree[3] / "new-entry").exists()


def test_l1_seed_merges_with_local_entries(build_tree):
    result, observed = run_build(build_tree, "on", "false")
    assert result.returncode == 0, result.stderr
    assert observed == {"compile": "1", "cache": ["candidate", "existing"], "cached_library": False}
    local = build_tree[3]
    assert (local / "existing").read_text() == "older OBS entry"
    assert (local / "new-entry").read_text() == "compiled"
    assert (build_tree[2] / "candidate").is_file()


def test_missing_l1_falls_back_to_source_compilation(build_tree):
    for directory in build_tree[2:4]:
        for path in directory.iterdir():
            path.unlink()
    result, observed = run_build(build_tree, "on", "false")
    assert result.returncode == 0, result.stderr
    assert observed["compile"] == "1"
    assert observed["cache"] == []
    assert (build_tree[0] / "vllm_ascend/new.so").is_file()


def test_install_failure_propagates(build_tree):
    build_tree[4]["TEST_BUILD_FAIL"] = "1"
    result, _ = run_build(build_tree, "on", "false")
    assert result.returncode == 17
