"""Expose the PR's prebuilt CodeGraph index to the real PTC review session."""

from pathlib import Path


def enable_codegraph(review):
    """Mount the stdio MCP server during DSH startup, ahead of model inference."""
    original = review.agent_patch
    relay = Path(__file__).with_name("codegraph_stdio.cjs").resolve()

    def patch_with_codegraph(workspace):
        head = str((workspace / "head").resolve())
        return [
            *original(workspace),
            {
                "insert": [
                    {
                        "id": "review-codegraph",
                        "name": "@deepseek-ai/dsh-mcp-client",
                        "config": {
                            "serverName": "codegraph",
                            "transport": "stdio",
                            "command": "node",
                            "args": [str(relay), head, "/tmp/replay-codegraph-events.jsonl"],
                            "cwd": head,
                            "env": {"CODEGRAPH_MCP_TOOLS": "explore,node,callers"},
                            "failOnStartupError": True,
                        },
                    }
                ]
            },
        ]

    review.agent_patch = patch_with_codegraph
