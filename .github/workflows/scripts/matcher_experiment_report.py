# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Collect independent matcher trials without treating unrelated failures as races."""

import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import regex as re

GROUPS = ("A original", "B current", "C sleep-5s")
TRIALS_PER_GROUP = 10
JOB_PATTERN = re.compile(r"^(A original|B current|C sleep-5s) / (\d+)$")
EXPENSIVE_STEPS = (
    "Run pre-commit",
    "Run mypy for Python 3.10, 3.11 and 3.12 in parallel",
)
UPSTREAM_SHA = "d476b009777343b1b5e4a3550e4a34f43108ed8e"
IMAGE_REFERENCE = "quay.nju.edu.cn/ascend-ci/vllm-ascend:lint"
MAX_REQUEST_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 60


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_api_bytes(url, token):
    """Never forward the GitHub token to the signed log-download host."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            try:
                with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code not in (301, 302, 303, 307, 308):
                    raise
                location = error.headers["Location"]
                if not location.startswith("https://"):
                    raise ValueError("Refusing a non-HTTPS log download") from error
                with urllib.request.urlopen(location, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return response.read()
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == MAX_REQUEST_ATTEMPTS:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("Request attempts exhausted")


def classify_job(job, log):
    match = JOB_PATTERN.fullmatch(job["name"])
    if match is None:
        raise ValueError(f"Unexpected trial name: {job['name']}")
    group, trial = match.groups()
    steps = job.get("steps", [])
    matcher_steps = [
        step for step in steps if "add-matcher" in step["name"] or step["name"] == "Register problem matchers"
    ]
    error_lines = [line for line in log.splitlines() if "##[error]" in line]
    missing = [
        line
        for line in error_lines
        if "Could not find file" in line and re.search(r"/(actionlint|markdownlint|mypy)\.json", line)
    ]
    failed = any(step["conclusion"] == "failure" for step in matcher_steps)
    expected_steps = 3 if group == "A original" else 1
    passed = len(matcher_steps) == expected_steps and all(step["conclusion"] == "success" for step in matcher_steps)
    if missing:
        outcome = "matcher_file_missing"
    elif failed:
        outcome = "matcher_other_failure"
    elif passed:
        outcome = "matcher_passed"
    else:
        outcome = "matcher_not_completed"
    runner_version = re.search(r"Current runner version: '([^']+)'", log)
    return {
        "group": group,
        "trial": int(trial),
        "job_id": job["id"],
        "url": job["html_url"],
        "job_conclusion": job["conclusion"],
        "matcher_outcome": outcome,
        "runner_name": job.get("runner_name"),
        "runner_version": runner_version.group(1) if runner_version else None,
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "copy_back_warning": "##[warning]Failed to copy _temp from pod" in log,
        "hook_error": any("custom container implementation failed" in line for line in error_lines),
        "failed_steps": [step["name"] for step in steps if step["conclusion"] == "failure"],
        "expensive_checks_skipped": all(
            any(step["name"] == name and step["conclusion"] == "skipped" for step in steps) for name in EXPENSIVE_STEPS
        ),
        "missing_file_errors": missing,
        "error_lines": error_lines,
    }


def collect_trial(job, api_url, token, output):
    try:
        log = get_api_bytes(f"{api_url}/actions/jobs/{job['id']}/logs", token).decode("utf-8-sig")
        (output / f"job-{job['id']}.log").write_text(log)
        return classify_job(job, log)
    except Exception as error:
        match = JOB_PATTERN.fullmatch(job["name"])
        return {
            "group": match.group(1),
            "trial": int(match.group(2)),
            "job_id": job["id"],
            "url": job["html_url"],
            "matcher_outcome": "collection_error",
            "error": str(error),
        }


def render_report(records, run_url):
    lines = [
        "# Matcher registration experiment",
        "",
        f"Run: {run_url}",
        f"Upstream baseline: `{UPSTREAM_SHA}`",
        f"Container image: `{IMAGE_REFERENCE}`",
        "",
        "Each group has 10 independent jobs, with max-parallel=1 and fail-fast=false.",
        "The groups run concurrently; pre-commit and mypy execution are skipped.",
        "Diagnostics only run after the observation. No matcher retries are performed.",
        "",
        "| Group | Samples | Matcher passed | File missing | Other failure | Not completed | Collection error |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in GROUPS:
        samples = [record for record in records if record["group"] == group]
        counts = [
            sum(record["matcher_outcome"] == outcome for record in samples)
            for outcome in (
                "matcher_passed",
                "matcher_file_missing",
                "matcher_other_failure",
                "matcher_not_completed",
                "collection_error",
            )
        ]
        lines.append(f"| {group} | {len(samples)} | " + " | ".join(map(str, counts)) + " |")
    lines.extend(["", "## Individual trials", ""])
    for record in records:
        details = record["matcher_outcome"]
        if record.get("copy_back_warning"):
            details += "; temp copy-back warning"
        if record.get("hook_error"):
            details += "; container hook error"
        if record.get("failed_steps"):
            details += "; failed steps: " + ", ".join(record["failed_steps"])
        lines.append(f"- [{record['group']} / {record['trial']}]({record['url']}): {details}")
    lines.extend(["", "Zero observed failures in ten trials do not establish that a variant is race-free.", ""])
    return "\n".join(lines)


def main():
    token = os.environ["GH_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    api_url = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repository}"
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    output = Path("matcher-experiment-results")
    output.mkdir(exist_ok=True)
    jobs = []
    page = 1
    while True:
        url = f"{api_url}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}"
        batch = json.loads(get_api_bytes(url, token))["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    (output / "jobs.json").write_text(json.dumps(jobs, indent=2))
    trials = [job for job in jobs if JOB_PATTERN.fullmatch(job["name"])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        records = list(executor.map(lambda job: collect_trial(job, api_url, token, output), trials))
    records.sort(key=lambda record: (record["group"], record["trial"]))
    (output / "results.json").write_text(json.dumps(records, indent=2))
    markdown = render_report(records, run_url)
    (output / "report.md").write_text(markdown)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(markdown)
    expected = {(group, trial) for group in GROUPS for trial in range(1, TRIALS_PER_GROUP + 1)}
    actual = {(record["group"], record["trial"]) for record in records}
    if actual != expected or len(records) != len(expected):
        raise RuntimeError("The report does not contain exactly one result for each of the 30 planned trials")
    if any(record["matcher_outcome"] == "collection_error" for record in records):
        raise RuntimeError("Some trial logs could not be collected; inspect results.json")
    if any(not record["expensive_checks_skipped"] for record in records):
        raise RuntimeError("Some jobs did not report both expensive checks as skipped")


if __name__ == "__main__":
    main()
