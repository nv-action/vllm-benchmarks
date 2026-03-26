from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github_adapter import GitHubCliAdapter


def test_list_open_pr_numbers_passes_label_to_gh_cli():
    calls = []

    def fake_runner(args):
        calls.append(args)
        return "[]"

    adapter = GitHubCliAdapter(runner=fake_runner)
    result = adapter.list_open_pr_numbers("nv-action/vllm-benchmarks", label="main2main")

    assert result == []
    assert "--label" in calls[0]
    label_idx = calls[0].index("--label")
    assert calls[0][label_idx + 1] == "main2main"


def test_list_open_pr_numbers_uses_custom_label():
    def fake_runner(args):
        label_idx = args.index("--label")
        assert args[label_idx + 1] == "custom-label"
        return '[{"number": 42}]'

    adapter = GitHubCliAdapter(runner=fake_runner)
    result = adapter.list_open_pr_numbers("repo", label="custom-label")

    assert result == [42]
