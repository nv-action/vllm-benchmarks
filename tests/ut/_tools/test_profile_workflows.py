from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def test_a3_profile_uses_matching_image_and_parser():
    workflow = yaml.load((WORKFLOWS / "schedule_nightly_test_a3.yaml").read_text(), Loader=yaml.BaseLoader)
    producer = workflow["jobs"]["multi-card-tests"]
    parser = workflow["jobs"]["parse-multi-card-profile"]
    assert producer["with"]["image"] == parser["with"]["image"]
    assert parser["with"]["ref"] == "${{ github.sha }}"
    assert parser["uses"] == "./.github/workflows/_e2e_profile_parse.yaml"


def test_a3_overlay_preserves_image_runtime():
    workflow = (WORKFLOWS / "_e2e_nightly_single_node.yaml").read_text()
    overlay = workflow.split("for file in \\", 1)[1].split("; do", 1)[0]
    assert "tools/profile.py" in overlay
    assert "tools/aisbench.py" in overlay
    assert "vllm_ascend/" not in overlay
