"""Build-time BLAS selection checks for the Faiss adapter."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "faiss_test.sh"


@pytest.mark.skipif(shutil.which("g++") is None, reason="C++ compiler unavailable")
def test_existing_mkl_is_discovered_and_linked(tmp_path: Path) -> None:
    root = tmp_path / "mkl"
    include = root / "include"
    library = root / "lib"
    include.mkdir(parents=True)
    library.mkdir()
    (include / "mkl.h").write_text(
        'extern "C" double cblas_ddot(int, const double*, int, const double*, int);\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["g++", "-shared", "-fPIC", "-x", "c++", "-", "-o", str(library / "libmkl_intel_lp64.so")],
        input='extern "C" double cblas_ddot(int, const double* x, int, const double* y, int) { return x[0]*y[0]; }\n',
        text=True,
        check=True,
    )
    for component in ("gnu_thread", "core"):
        (library / f"libmkl_{component}.so").symlink_to("libmkl_intel_lp64.so")

    env = os.environ | {
        "EXPECTED_ARCH": "x86_64",
        "MKLROOT": str(root),
        "PERF_WORK_DIR": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }
    completed = subprocess.run(
        ["bash", "-c", 'source "$1"; prepare_math_library', "faiss-test", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"using Intel oneMKL at {root}" in completed.stdout


def test_incomplete_mkl_is_not_accepted(tmp_path: Path) -> None:
    root = tmp_path / "mkl"
    (root / "include").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "include" / "mkl.h").touch()
    completed = subprocess.run(
        ["bash", "-c", 'source "$1"; locate_mkl', "faiss-test", str(SCRIPT)],
        env=os.environ | {"MKLROOT": str(root), "PERF_WORK_DIR": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("hnsw", ["hnsw"]),
        ("all", ["hnsw", "ivfpq", "ivfpqfs", "pqfs", "ivfflat", "ivfrabitq", "ivfrabitqfs"]),
    ],
)
def test_benchmark_profile_selects_build_targets(profile: str, expected: list[str]) -> None:
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; configure_benchmark_profile; printf "%s\\n" "${ACTIVE_ALGORITHMS[@]}"',
            "faiss-test",
            str(SCRIPT),
        ],
        env=os.environ | {"FAISS_BENCH_PROFILE": profile},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == expected


def test_invalid_benchmark_profile_fails() -> None:
    completed = subprocess.run(
        ["bash", "-c", 'source "$1"; configure_benchmark_profile', "faiss-test", str(SCRIPT)],
        env=os.environ | {"FAISS_BENCH_PROFILE": "other"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 10
