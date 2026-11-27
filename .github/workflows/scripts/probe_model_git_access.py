"""Check that the real review model can read the prepared PR with its Bash tool."""

import json
import secrets
import shlex
import tempfile
from pathlib import Path
from unittest.mock import patch

import ai_review

GIT_PROBE = """\
set -eu
probe_dir=$(dirname "$0")
base=$(git -c safe.directory="$PWD/base" -C base rev-parse HEAD)
head=$(git -c safe.directory="$PWD/head" -C head rev-parse HEAD)
{
    pwd
    printf '%s\\n' "$base" "$head"
    git -c safe.directory="$PWD" diff --no-ext-diff --no-textconv --name-only "$base" "$head" --
} > "$probe_dir/receipt"
cat "$probe_dir/challenge"
"""


def main() -> None:
    """Compare the Bash receipt with the snapshot and Git diff collected by prepare."""
    workspace = Path.cwd()
    request = json.loads(Path("/tmp/ai-review/request.json").read_text())
    expected = [str(workspace), request["merge_base"], request["state"]["head"], *request["files"]]

    with tempfile.TemporaryDirectory(prefix="ai-review-git-access-") as directory:
        probe_dir = Path(directory)
        script = probe_dir / "probe.sh"
        script.write_text(GIT_PROBE)
        challenge = secrets.token_hex(16)
        (probe_dir / "challenge").write_text(challenge + "\n")
        prompt = (
            "Verify Bash access in this CI job. Call your Bash tool once with the following exact command. "
            "The script reads the PR commits and diff and prints a one-time token. "
            "Return that token as your final answer.\n\n"
            f"bash {shlex.quote(str(script))}"
        )
        with patch.object(ai_review, "build_prompt", return_value=prompt):
            answer = ai_review.run_agent(workspace, request)

        receipt = (probe_dir / "receipt").read_text().splitlines()
        if receipt != expected:
            raise SystemExit("Model Bash receipt did not match the prepared PR snapshot.")
        if challenge not in answer:
            raise SystemExit("The model did not return the token printed by Bash.")

    print(f"Model Bash verified: cwd={receipt[0]}, base={receipt[1]}, head={receipt[2]}")
    print("Changed paths:", *receipt[3:], sep="\n")
    print("The model returned the one-time token from its Bash tool.")


if __name__ == "__main__":
    main()
