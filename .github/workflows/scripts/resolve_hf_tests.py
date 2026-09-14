#
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
#
"""Resolve high-frequency (HF) nightly test cases for daytime regression runs.

Reads an HF config YAML (either the repo's ``hf_nightly_config.yaml`` or an
inline YAML string materialized by the workflow) and ``nightly_config.yaml``,
then resolves the ``hf_test_cases`` workflow input into per-SOC,
comma-separated test-name lists that are dispatched to the
``schedule_nightly_test_<soc>.yaml`` workflows.

Every selected ``name`` is validated against ``nightly_config.yaml`` so an HF
entry referencing a removed test is surfaced as a warning instead of silently
dispatching a no-op run.

Supported ``--test-cases`` formats:
  - JSON dict:      {"a2": ["multi-node-qwen3-235b-dp"], "a3": ["hf-all"]}
                    Exact per-SOC control; absent SOCs are not dispatched.
  - hf-<soc> token: hf-all / hf-a2 / hf-a3 / ...
                    Expands to every HF name configured for that SOC.
  - comma-separated test names:
                    multi-node-qwen3-235b-dp,deepseek-r1-0528-w8a8
                    Matched by test name across all SOCs.

Writes GITHUB_OUTPUT (or stdout when unset):
  - has_hf_<soc>=true|false
  - hf_<soc>_tests=<comma-separated test names>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

try:
    import yaml
except ImportError:
    subprocess.check_call(["pip3", "install", "pyyaml", "-q"])
    import yaml

# SOCs that map 1:1 to the schedule_nightly_test_<soc>.yaml workflows.
SUPPORTED_SOCS = ("a2", "a3", "a3-560t", "a5")


def _load_yaml(path):
    """Load a YAML file, tolerating an empty/whitespace file."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _collect_ordered_names(node, ordered, seen):
    """Recursively collect ``name`` fields, preserving declaration order."""
    if isinstance(node, dict):
        for value in node.values():
            _collect_ordered_names(value, ordered, seen)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                name = item["name"]
                if name not in seen:
                    seen.add(name)
                    ordered.append(name)
            elif isinstance(item, (dict, list)):
                _collect_ordered_names(item, ordered, seen)


def _names_by_soc(config):
    """Return {soc: [ordered names]} for every SOC present in the config."""
    result = {}
    for soc in SUPPORTED_SOCS:
        soc_block = config.get(soc)
        if not isinstance(soc_block, dict):
            continue
        ordered: list[str] = []
        _collect_ordered_names(soc_block, ordered, set())
        if ordered:
            result[soc] = ordered
    return result


def _hf_names_by_soc(hf_config):
    """Read the ``high_frequency`` block and return {soc: [ordered names]}."""
    block = hf_config.get("high_frequency", hf_config)
    return _names_by_soc(block)


def _nightly_ordered_by_soc(nightly_config):
    """Return {soc: [ordered names]} from nightly_config.yaml (ordered)."""
    result = {}
    for soc in SUPPORTED_SOCS:
        soc_block = nightly_config.get(soc)
        if not isinstance(soc_block, dict):
            continue
        ordered: list[str] = []
        _collect_ordered_names(soc_block, ordered, set())
        if ordered:
            result[soc] = ordered
    return result


def _nightly_names_by_soc(nightly_config):
    """Return {soc: set(names)} from nightly_config.yaml (all sections)."""
    ordered = _nightly_ordered_by_soc(nightly_config)
    return {soc: set(names) for soc, names in ordered.items()}


def _expand_spec(raw, hf_by_soc):
    """Parse the ``hf_test_cases`` input into {soc: [requested tokens]}.

    Returns (spec, unknown_warnings) where spec maps each SOC to the list of
    requested tokens (test names or the ``hf-all`` shortcut).
    """
    raw = (raw or "").strip()
    spec = {}
    if not raw or raw == "hf-all":
        for soc in hf_by_soc:
            spec[soc] = ["hf-all"]
        return spec

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"::error::Invalid JSON for hf_test_cases: {exc}")
        if not isinstance(parsed, dict):
            raise SystemExit("::error::hf_test_cases JSON must be an object "
                             'mapping SOC to an array of test names.')
        for soc, requested in parsed.items():
            if not isinstance(requested, (str, list)):
                print(f"::warning::hf_test_cases value for '{soc}' is not a "
                      "string or list; skipped.")
                continue
            spec[str(soc)] = [requested] if isinstance(requested, str) else requested
        return spec

    # Comma-separated tokens: hf-<soc> shortcuts and/or plain test names.
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        if token.startswith("hf-") or token == "hf-all":
            if token == "hf-all":
                for soc in hf_by_soc:
                    spec.setdefault(soc, []).append("hf-all")
            else:
                soc = token[3:]
                if soc in hf_by_soc:
                    spec.setdefault(soc, []).append("hf-all")
                else:
                    print(f"::warning::Unknown HF soc '{soc}' in '{token}'; skipped.")
        else:
            matched = False
            for soc, names in hf_by_soc.items():
                if token in names:
                    spec.setdefault(soc, []).append(token)
                    matched = True
            if not matched:
                print(f"::warning::HF test name '{token}' is not configured for "
                      "any SOC; skipped.")
    return spec


def _resolve(raw, hf_by_soc, nightly_by_soc):
    """Return {soc: [validated, ordered test names]} to dispatch."""
    spec = _expand_spec(raw, hf_by_soc)
    known_all = set()
    for nightly_names in nightly_by_soc.values():
        known_all |= nightly_names

    result = {}
    for soc, requested in spec.items():
        if soc not in hf_by_soc:
            print(f"::warning::SOC '{soc}' has no HF tests configured; skipped.")
            continue
        hf_ordered = hf_by_soc[soc]
        hf_names = set(hf_ordered)
        want = set()
        for item in requested:
            if item == "hf-all":
                want |= hf_names
            elif item in hf_names:
                want.add(item)
            else:
                print(f"::warning::HF test name '{item}' is not configured for "
                      f"SOC '{soc}'; skipped.")
        missing = want - known_all
        for name in sorted(missing):
            print(f"::warning::HF test name '{name}' not found in "
                  "nightly_config.yaml; skipped.")
        want -= missing
        if want:
            result[soc] = [name for name in hf_ordered if name in want]
    return result


def _resolve_direct(raw, nightly_ordered):
    """Resolve ``hf_test_cases`` directly against nightly_config.yaml.

    Used when ``hf_test_cases`` is non-empty: the input IS the test spec and
    ``hf_config_yaml`` is not consulted. Names are validated against
    ``nightly_config.yaml`` and routed to the SOC where they are defined.

    ``nightly_ordered``: {soc: [ordered names]}.
    """
    nightly_sets = {soc: set(names) for soc, names in nightly_ordered.items()}

    if raw.startswith("{"):
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"::error::Invalid JSON for hf_test_cases: {exc}")
        if not isinstance(spec, dict):
            raise SystemExit("::error::hf_test_cases JSON must be an object "
                             'mapping SOC to an array of test names.')
        result = {}
        for soc, requested in spec.items():
            if soc not in nightly_sets:
                print(f"::warning::SOC '{soc}' has no tests in nightly_config.yaml; skipped.")
                continue
            all_names = nightly_sets[soc]
            if isinstance(requested, str):
                requested = [requested]
            want = set()
            for item in requested:
                if item == "hf-all":
                    want |= all_names
                elif item in all_names:
                    want.add(item)
                else:
                    print(f"::warning::Test name '{item}' not found for SOC '{soc}' "
                          "in nightly_config.yaml; skipped.")
            if want:
                result[soc] = [name for name in nightly_ordered[soc] if name in want]
        return result

    # Comma-separated tokens: hf-<soc> shortcuts and/or plain test names.
    spec = {}
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        if token == "hf-all" or token.startswith("hf-"):
            if token == "hf-all":
                for soc in nightly_sets:
                    spec[soc] = "hf-all"
            else:
                soc = token[3:]
                if soc in nightly_sets:
                    spec[soc] = "hf-all"
                else:
                    print(f"::warning::Unknown SOC '{soc}' in '{token}'; skipped.")
        else:
            matched = False
            for soc, names in nightly_sets.items():
                if token in names:
                    spec.setdefault(soc, set()).add(token)
                    matched = True
            if not matched:
                print(f"::warning::Test name '{token}' not found in "
                      "nightly_config.yaml for any SOC; skipped.")

    result = {}
    for soc, requested in spec.items():
        if requested == "hf-all":
            result[soc] = list(nightly_ordered[soc])
        else:
            valid = [name for name in nightly_ordered[soc] if name in requested]
            if valid:
                result[soc] = valid
    return result


def _emit_output(resolved):
    """Write per-SOC outputs to GITHUB_OUTPUT, or stdout when unset."""
    lines = []
    for soc in SUPPORTED_SOCS:
        key = soc.replace("-", "_")
        names = resolved.get(soc, [])
        lines.append(f"has_hf_{key}={str(bool(names)).lower()}")
        lines.append(f"hf_{key}_tests={','.join(names)}")
    text = "\n".join(lines) + "\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


def main():
    parser = argparse.ArgumentParser(
        description="Resolve high-frequency nightly test cases for dispatch.",
    )
    parser.add_argument(
        "--hf-config",
        required=True,
        help="Path to the HF config YAML (repo default or inline materialized).",
    )
    parser.add_argument(
        "--nightly-config",
        required=True,
        help="Path to nightly_config.yaml used to validate test names.",
    )
    parser.add_argument(
        "--test-cases",
        default="",
        help="hf_test_cases input. Empty = use all tests from --hf-config. "
        "Non-empty = direct spec: JSON dict, hf-<soc> tokens, or names.",
    )
    args = parser.parse_args()

    raw = (args.test_cases or "").strip()
    nightly_ordered = _nightly_ordered_by_soc(_load_yaml(args.nightly_config))
    nightly_by_soc = {soc: set(names) for soc, names in nightly_ordered.items()}

    if not raw:
        # Empty input: use every HF test declared in hf_config_yaml.
        hf_by_soc = _hf_names_by_soc(_load_yaml(args.hf_config))
        resolved = _resolve("hf-all", hf_by_soc, nightly_by_soc)
        print("::notice::hf_test_cases is empty; using all tests from hf_config_yaml")
    else:
        # Non-empty input: hf_test_cases is the spec itself (no hf_config).
        resolved = _resolve_direct(raw, nightly_ordered)
        print("::notice::hf_test_cases is set; resolved directly against nightly_config.yaml")

    for soc, names in sorted(resolved.items()):
        print(f"[{soc}] dispatch {len(names)} test(s): {', '.join(names)}")
    _emit_output(resolved)


if __name__ == "__main__":
    main()
