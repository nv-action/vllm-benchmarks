#!/usr/bin/env bash
# Self-hosted runners may ship a python3 without pip and without ensurepip.
# Bootstrap pip from the PyPI mirror by unpacking the pip wheel into a private
# directory, and print that directory so the caller can prepend it to
# PYTHONPATH. Print nothing when python3 already has pip.

set -euo pipefail

if python3 -m pip --version >/dev/null 2>&1; then
    exit 0
fi

index="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
pip_home="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/boostkit-pip"
mkdir -p "${pip_home}"

wheel_url=$(curl -fsSL --max-time 30 "${index}/pip/" | python3 -c '
import re
import sys
from urllib.parse import urljoin

html = sys.stdin.read()
links = re.findall(r"href=\"([^\"]+pip-[0-9.]+-py3-none-any\.whl(?:#[^\"]*)?)\"", html)
if not links:
    sys.exit("no pip wheel found on the package index")
print(urljoin(sys.argv[1], links[-1].split("#")[0]))
' "${index}/pip/")

wheel="${pip_home}/pip.whl"
curl -fsSL --max-time 120 "${wheel_url}" -o "${wheel}"
python3 -m zipfile -e "${wheel}" "${pip_home}"

PYTHONPATH="${pip_home}" python3 -m pip --version >/dev/null
echo "${pip_home}"
