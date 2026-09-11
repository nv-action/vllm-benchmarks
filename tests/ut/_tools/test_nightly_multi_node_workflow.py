import re
from pathlib import Path

WORKFLOW_PATH = Path(".github/workflows/_e2e_nightly_multi_node.yaml")
K8S_LABEL_MAX_LENGTH = 63
LWS_NAME_FIXED_LENGTH = len("vllm--") + 6
LWS_CONTROLLER_LABEL_SUFFIX_LENGTH = len("-0-") + 10


def test_generated_lws_controller_label_fits_k8s_limit() -> None:
    workflow = WORKFLOW_PATH.read_text()
    match = re.search(r"^\s*MAX_LWS_SUFFIX_LENGTH=(\d+)$", workflow, re.MULTILINE)

    assert match is not None
    max_lws_suffix_length = int(match.group(1))
    assert LWS_NAME_FIXED_LENGTH + max_lws_suffix_length + LWS_CONTROLLER_LABEL_SUFFIX_LENGTH <= K8S_LABEL_MAX_LENGTH
    assert 'cut -c "1-${MAX_LWS_SUFFIX_LENGTH}"' in workflow
    assert "LWS_CONTROLLER_LABEL_SUFFIX" in workflow
