from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from tools.ci_inference_smoke import SMOKE_MAX_TOKENS, SMOKE_REQUESTS, run_inference_smoke


def test_smoke_success():
    client = Mock()
    client.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(text="Paris", finish_reason="stop")]
    )
    run_inference_smoke(client, "model")
    assert client.completions.create.call_count == SMOKE_REQUESTS
    for call in client.completions.create.call_args_list:
        assert call.kwargs["max_tokens"] == SMOKE_MAX_TOKENS
        assert call.kwargs["model"] == "model"


@pytest.mark.parametrize(
    "choices",
    [[], [SimpleNamespace(text=" ", finish_reason="stop")], [SimpleNamespace(text="Paris", finish_reason="error")]],
)
def test_smoke_rejects_invalid_response(choices):
    client = Mock()
    client.completions.create.return_value = SimpleNamespace(choices=choices)
    with pytest.raises(AssertionError):
        run_inference_smoke(client, "model")


def test_smoke_propagates_request_failure():
    client = Mock()
    client.completions.create.side_effect = RuntimeError("request failed")
    with pytest.raises(RuntimeError, match="request failed"):
        run_inference_smoke(client, "model")


def test_private_validation_configuration():
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load((root / ".github/workflows/schedule_nightly_test_a2.yaml").read_text(), Loader=yaml.BaseLoader)
    config = workflow["jobs"]["multi-node-tests"]["with"]
    assert config["runner"] == "linux-aarch64-a2b3-0"
    assert "@sha256:a99d00ed" in config["image"]
    assert "github.repository" in config["vllm_ascend_remote_url"]
    case = yaml.safe_load((root / "tests/e2e/cases/models/configs/Qwen/Qwen3-235B-A22B-A2.yaml").read_text())
    assert case["num_nodes"] == 2
    assert case["npu_per_node"] == 8
    assert case["benchmarks"] == {}
    assert len(case["deployment"]) == 2
    for path in (root / ".github/workflows").iterdir():
        if path.suffix in (".yaml", ".yml"):
            events = yaml.load(path.read_text(), Loader=yaml.BaseLoader)["on"]
            assert not set(events).intersection(
                {"schedule", "push", "pull_request", "pull_request_target", "workflow_run"}
            )
