from pathlib import Path


def test_systemd_service_uses_correct_entrypoint():
    text = Path("deploy/systemd/vllm-benchmarks-orchestrator.service").read_text()
    assert "service_main.py" in text
    assert "EnvironmentFile=/etc/vllm-benchmarks-orchestrator/orchestrator.env" in text
    assert "TimeoutStopSec=60" in text


def test_env_example_contains_required_variables():
    text = Path("deploy/systemd/orchestrator.env.example").read_text()
    for var in [
        "GITHUB_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "STATE_PATH",
        "POLL_INTERVAL",
        "REPO",
        "MCP_HOST",
        "MCP_PORT",
    ]:
        assert var in text
