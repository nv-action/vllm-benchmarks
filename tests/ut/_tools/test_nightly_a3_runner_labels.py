from pathlib import Path

import yaml

A3_CONTROLLER_RUNNER = "linux-aarch64-a3-0"
A3_TEST_RUNNERS = {
    "linux-aarch64-a3-2",
    "linux-aarch64-a3-4",
    "linux-aarch64-a3-8",
    "linux-aarch64-a3-16",
}


def _load_yaml(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def test_nightly_a3_workflow_uses_repository_controller_scale_set() -> None:
    workflow = _load_yaml(".github/workflows/schedule_nightly_test_a3.yaml")
    jobs = workflow["jobs"]

    assert jobs["parse-trigger"]["runs-on"] == A3_CONTROLLER_RUNNER
    assert jobs["clear-pre-logs"]["runs-on"] == A3_CONTROLLER_RUNNER
    assert jobs["remove-taints"]["runs-on"] == A3_CONTROLLER_RUNNER


def test_nightly_a3_matrix_uses_repository_test_scale_sets() -> None:
    config = _load_yaml(".github/workflows/configs/nightly_config.yaml")

    for group_name in ("single_node", "multi_card"):
        for test_config in config["a3"][group_name]["test_config"]:
            assert test_config["os"] in A3_TEST_RUNNERS


def test_actionlint_knows_repository_a3_scale_sets() -> None:
    actionlint = _load_yaml(".github/actionlint.yaml")
    labels = set(actionlint["self-hosted-runner"]["labels"])

    assert A3_CONTROLLER_RUNNER in labels
    assert labels >= A3_TEST_RUNNERS
