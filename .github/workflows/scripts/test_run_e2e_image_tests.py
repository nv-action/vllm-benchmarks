# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("run_e2e_image_tests.sh")


@pytest.mark.parametrize("targets,expected_status", [(["ok", "bad", "last"], 1), (["ok", "last"], 0)])
def test_runner_preserves_every_result_and_continues_after_failure(tmp_path, targets, expected_status):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    programs = {
        "python3": (
            "import os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:3] == ['-m', 'pytest']:\n"
            "    print('output for ' + sys.argv[-1], flush=True)\n"
            "    xml = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--junitxml='))\n"
            "    Path(xml).write_text('<testsuites/>')\n"
            "    raise SystemExit(1 if sys.argv[-1] == 'bad' else 0)\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        ),
        # macOS does not ship GNU setsid/tail; preserve Linux process-group and
        # tail lifetime semantics while testing the real runner shell script.
        "setsid": "import os, sys\nos.setsid()\nos.execvp(sys.argv[1], sys.argv[1:])\n",
        "tail": (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "pid = int(sys.argv[1].split('=')[1])\n"
            "while True:\n"
            "    try: os.kill(pid, 0)\n"
            "    except ProcessLookupError: break\n"
            "    time.sleep(0.01)\n"
            "print(Path(sys.argv[-1]).read_text(), end='')\n"
        ),
    }
    for name, content in programs.items():
        path = binaries / name
        path.write_text(f"#!{sys.executable}\n{content}")
        path.chmod(0o755)
    output = tmp_path / "results"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(output), *targets],
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected_status, result.stderr
    with (output / "results.tsv").open() as stream:
        records = list(csv.reader(stream, delimiter="\t"))
    assert [record[0] for record in records] == targets
    for index, (target, code, seconds, log) in enumerate(records, 1):
        assert int(code) == int(target == "bad")
        assert int(seconds) >= 0
        assert Path(log) == output / f"{index}.log"
        assert f"output for {target}" in Path(log).read_text()
        assert (output / f"{index}.xml").is_file()
