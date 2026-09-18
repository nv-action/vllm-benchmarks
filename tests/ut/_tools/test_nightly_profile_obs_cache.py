from pathlib import Path

import pytest
import yaml

OBS_CACHE_ENV = {
    "RUNS_ON_S3_BUCKET_CACHE": "obs-guiiyang1-ascend-test",
    "RUNS_ON_S3_BUCKET_ENDPOINT": "https://obs.cn-southwest-2.myhuaweicloud.com",
    "RUNS_ON_S3_FORCE_PATH_STYLE": "false",
    "AWS_S3_FORCE_PATH_STYLE": "false",
    "AWS_REGION": "cn-southwest-2",
    "RUNS_ON_RUNNER_NAME": "",
    "AWS_ACCESS_KEY_ID": "${{ secrets.AWS_ACCESS_KEY_ID }}",
    "AWS_SECRET_ACCESS_KEY": "${{ secrets.AWS_SECRET_ACCESS_KEY }}",
}
OBS_CACHE_CONDITION = "inputs.soc_version == 'a2' || inputs.soc_version == 'a3'"
LEGACY_UPLOAD_CONDITION = "inputs.soc_version != 'a2' && inputs.soc_version != 'a3'"


def _load_workflow(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


@pytest.mark.parametrize(
    (
        "workflow_path",
        "job_name",
        "archive_path",
        "cache_step_name",
        "direct_step_name",
        "direct_working_directory",
        "legacy_obs_step_name",
        "github_step_name",
    ),
    [
        (
            ".github/workflows/_e2e_nightly_single_node.yaml",
            "e2e-nightly",
            "/tmp/profile-output.tar.gz",
            "Save profiling artifacts to OBS cache (A2/A3)",
            "Upload profiling archive directly to OBS (A2/A3)",
            "/vllm-workspace/vllm-ascend",
            "Upload profiling artifacts (OBS)",
            "Upload profiling artifacts (GitHub Artifacts)",
        ),
        (
            ".github/workflows/_e2e_nightly_multi_node.yaml",
            "e2e",
            "/tmp/profile-results.tar.gz",
            "Save profiling results to OBS cache (A2/A3)",
            "Upload profiling archive directly to OBS (A2/A3)",
            None,
            "Upload profiling results (OBS)",
            "Upload profiling results (GitHub Artifacts)",
        ),
    ],
)
def test_a2_a3_profiling_uses_direct_obs_upload_only(
    workflow_path: str,
    job_name: str,
    archive_path: str,
    cache_step_name: str,
    direct_step_name: str,
    direct_working_directory: str | None,
    legacy_obs_step_name: str,
    github_step_name: str,
) -> None:
    workflow = _load_workflow(workflow_path)
    step_list = workflow["jobs"][job_name]["steps"]
    steps = {step["name"]: step for step in step_list}

    cache_step = steps[cache_step_name]
    assert cache_step["uses"] == "runs-on/cache/save@v5"
    assert cache_step["continue-on-error"] is True
    assert cache_step["env"] == OBS_CACHE_ENV
    assert cache_step["with"]["path"] == archive_path
    assert cache_step["if"] == "${{ false }}"

    direct_step = steps[direct_step_name]
    assert direct_step["continue-on-error"] is True
    assert direct_step["env"]["RUNS_ON_S3_BUCKET_CACHE"] == OBS_CACHE_ENV["RUNS_ON_S3_BUCKET_CACHE"]
    assert direct_step["env"]["RUNS_ON_S3_BUCKET_ENDPOINT"] == OBS_CACHE_ENV["RUNS_ON_S3_BUCKET_ENDPOINT"]
    assert direct_step["env"]["AWS_REGION"] == OBS_CACHE_ENV["AWS_REGION"]
    assert direct_step["env"]["AWS_ACCESS_KEY_ID"] == OBS_CACHE_ENV["AWS_ACCESS_KEY_ID"]
    assert direct_step["env"]["AWS_SECRET_ACCESS_KEY"] == OBS_CACHE_ENV["AWS_SECRET_ACCESS_KEY"]
    assert direct_step["env"]["PROFILE_ARCHIVE"] == archive_path
    assert direct_step["env"]["OBS_OBJECT_KEY"].startswith("nightly-profiling/${{ github.repository }}/")
    assert "tests/e2e/nightly/scripts/upload_profile_to_obs.py" in direct_step["run"]
    assert OBS_CACHE_CONDITION in direct_step["if"]
    assert direct_step.get("working-directory") == direct_working_directory

    step_names = [step["name"] for step in step_list]
    assert step_names.index(cache_step_name) + 1 == step_names.index(direct_step_name)

    legacy_obs_step = steps[legacy_obs_step_name]
    assert legacy_obs_step["uses"] == "ascend-gha-runners/artifact/upload@v0.3"
    assert LEGACY_UPLOAD_CONDITION in legacy_obs_step["if"]

    github_step = steps[github_step_name]
    assert github_step["uses"] == "actions/upload-artifact@v7"
    assert LEGACY_UPLOAD_CONDITION in github_step["if"]


def test_direct_obs_upload_uses_s3_compatible_checksum_settings() -> None:
    uploader = Path("tests/e2e/nightly/scripts/upload_profile_to_obs.py").read_text()

    assert 'request_checksum_calculation="when_required"' in uploader
    assert 'response_checksum_validation="when_required"' in uploader
    assert '"payload_signing_enabled": False' in uploader


@pytest.mark.parametrize(
    ("workflow_path", "job_names"),
    [
        (
            ".github/workflows/schedule_nightly_test_a2.yaml",
            ("multi-node-tests", "single-node-tests"),
        ),
        (
            ".github/workflows/schedule_nightly_test_a3.yaml",
            (
                "multi-node-tests",
                "double-node-tests",
                "single-node-tests",
                "multi-card-tests",
            ),
        ),
    ],
)
def test_nightly_workflows_forward_obs_cache_secrets(
    workflow_path: str,
    job_names: tuple[str, ...],
) -> None:
    workflow = _load_workflow(workflow_path)

    for job_name in job_names:
        secrets = workflow["jobs"][job_name]["secrets"]
        assert secrets["AWS_ACCESS_KEY_ID"] == "${{ secrets.AWS_ACCESS_KEY_ID }}"
        assert secrets["AWS_SECRET_ACCESS_KEY"] == "${{ secrets.AWS_SECRET_ACCESS_KEY }}"
