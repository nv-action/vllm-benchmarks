"""Keep the private Sysbench binary paired with its own Lua workloads."""

import os
import subprocess
from pathlib import Path

import pytest


def test_run_sysbench_uses_private_lua_workload(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "mysql_test.sh"
    fake_sysbench = tmp_path / "sysbench"
    fake_sysbench.write_text(
        '#!/bin/sh\nprintf "LUA_PATH=%s\\n" "$LUA_PATH"\n'
        'printf "ARG=%s\\n" "$@"\n',
        encoding="utf-8",
    )
    fake_sysbench.chmod(0o755)
    lua_directory = tmp_path / "src" / "lua"
    results = tmp_path / "results"
    environment = os.environ | {
        "TEST_SCRIPT": str(script),
        "TEST_BIN": str(fake_sysbench),
        "TEST_LUA_DIR": str(lua_directory),
        "TEST_RESULTS_DIR": str(results),
    }
    completed = subprocess.run(
        [
            "bash", "-c",
            'source "$TEST_SCRIPT"; SYSBENCH_BIN="$TEST_BIN"; '
            'SYSBENCH_LUA_DIR="$TEST_LUA_DIR"; RESULTS_DIR="$TEST_RESULTS_DIR"; '
            'mkdir -p "$RESULTS_DIR/sysbench-runs"; '
            'run_sysbench read_write 2 0 prepare',
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"LUA_PATH={lua_directory}/?.lua;;" in completed.stdout
    assert f"ARG={lua_directory}/oltp_read_write.lua" in completed.stdout
    assert "ARG=prepare" in completed.stdout
    assert "ARG=--table_size=10000000" in completed.stdout
    assert "ARG=--tables=64" in completed.stdout


@pytest.mark.parametrize("mode", ["prepare", "benchmark"])
def test_mysql_launch_sets_and_checks_prepared_statement_limit(tmp_path: Path, mode: str) -> None:
    script = Path(__file__).resolve().parents[1] / "mysql_test.sh"
    fake_mysqld = tmp_path / "mysqld"
    fake_mysqld.write_text('#!/bin/sh\nprintf "ARG=%s\\n" "$@"\n', encoding="utf-8")
    fake_mysqld.chmod(0o755)
    fake_mysql = tmp_path / "mysql"
    fake_mysql.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_EFFECTIVE_LIMIT"\n', encoding="utf-8")
    fake_mysql.chmod(0o755)
    fake_mysqladmin = tmp_path / "mysqladmin"
    fake_mysqladmin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_mysqladmin.chmod(0o755)
    environment = os.environ | {
        "TEST_SCRIPT": str(script),
        "TEST_MYSQLD": str(fake_mysqld),
        "TEST_MYSQL": str(fake_mysql),
        "TEST_MYSQLADMIN": str(fake_mysqladmin),
        "TEST_MODE": mode,
        "TEST_EFFECTIVE_LIMIT": "1048576",
        "TEST_ROOT": str(tmp_path),
    }
    command = (
        'source "$TEST_SCRIPT"; MYSQLD_BIN="$TEST_MYSQLD"; MYSQL_BIN="$TEST_MYSQL"; '
        'MYSQLADMIN_BIN="$TEST_MYSQLADMIN"; MYSQL_BASE_DIR="$TEST_ROOT"; '
        'DATADIR="$TEST_ROOT/data"; MYSQL_TMP_DIR="$TEST_ROOT/tmp"; '
        'SOCKET_PATH="$TEST_ROOT/mysql.sock"; PID_FILE="$TEST_ROOT/mysql.pid"; '
        'ERR_LOG="$TEST_ROOT/mysql.err"; MYSQL_HOST=127.0.0.1; MYSQL_PORT=3307; '
        'MYSQL_DB_USER=root; MYSQL_PASSWORD=test-password; '
        'launch_mysql_server "$TEST_MODE"'
    )
    completed = subprocess.run(
        ["bash", "-c", command], env=environment, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert "ARG=--max-prepared-stmt-count=1048576" in completed.stdout
    assert "mysqld is ready" in completed.stdout

    environment["TEST_EFFECTIVE_LIMIT"] = "16382"
    completed = subprocess.run(
        ["bash", "-c", command], env=environment, capture_output=True, text=True
    )
    assert completed.returncode != 0
    assert "max_prepared_stmt_count is 16382, expected 1048576" in completed.stdout
