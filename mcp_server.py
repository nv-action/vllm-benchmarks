from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import time

_START_TIME = time.time()

_TOOLS = [
    {
        "name": "orchestrator_list_prs",
        "description": "List open PRs with a given label",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "label": {"type": "string", "default": "main2main"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "orchestrator_get_pr_state",
        "description": "Get orchestrator state for a tracked PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "orchestrator_get_health",
        "description": "Get service health status",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "orchestrator_run_once",
        "description": "Run one reconciliation cycle for all tracked PRs",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "orchestrator_reconcile_pr",
        "description": "Reconcile a single PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "orchestrator_register_pr",
        "description": "Register a PR from its comment metadata",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
]


class McpOrchestrator:
    """Wraps orchestrator service as MCP tools."""

    def __init__(self, service, store, github, service_lock: asyncio.Lock, poll_state: dict):
        self._service = service
        self._store = store
        self._github = github
        self._lock = service_lock
        self._poll_state = poll_state

    def get_tools(self) -> list[dict]:
        return list(_TOOLS)

    async def handle_tool_call(self, name: str, arguments: dict) -> str:
        try:
            if name == "orchestrator_list_prs":
                result = await asyncio.to_thread(
                    self._github.list_open_pr_numbers,
                    arguments["repo"],
                    label=arguments.get("label", "main2main"),
                )
                return json.dumps(result)

            if name == "orchestrator_get_pr_state":
                state = self._store.get(arguments["repo"], arguments["pr_number"])
                return json.dumps(asdict(state) if state else None)

            if name == "orchestrator_get_health":
                data = self._store.load_all() if self._store.path.exists() else {}
                terminal_jobs = data.get("_terminal_jobs", [])
                pending = sum(1 for job in terminal_jobs if job.get("status") == "pending")
                pr_count = sum(1 for key in data if not key.startswith("_"))
                return json.dumps(
                    {
                        "state_file_exists": self._store.path.exists(),
                        "state_file_path": str(self._store.path),
                        "tracked_pr_count": pr_count,
                        "terminal_jobs_pending": pending,
                        "last_poll_time": self._poll_state.get("last_poll_time"),
                        "last_poll_result": self._poll_state.get("last_poll_result"),
                        "uptime_seconds": round(time.time() - _START_TIME, 1),
                    }
                )

            if name == "orchestrator_run_once":
                async with self._lock:
                    result = await asyncio.to_thread(self._service.run_once, arguments["repo"])
                return json.dumps(result)

            if name == "orchestrator_reconcile_pr":
                async with self._lock:
                    result = await asyncio.to_thread(
                        self._service.reconcile,
                        arguments["repo"],
                        arguments["pr_number"],
                    )
                return json.dumps(result)

            if name == "orchestrator_register_pr":
                async with self._lock:
                    result = await asyncio.to_thread(
                        self._service.register_from_pr_comment,
                        arguments["repo"],
                        arguments["pr_number"],
                    )
                return json.dumps(result)

            return json.dumps({"error": f"unknown tool: {name}"})
        except Exception as exc:
            return json.dumps({"error": type(exc).__name__, "message": str(exc)})


def build_mcp_server(service, store, github, service_lock: asyncio.Lock, poll_state: dict) -> McpOrchestrator:
    return McpOrchestrator(service, store, github, service_lock, poll_state)


def create_mcp_protocol_server(orchestrator: McpOrchestrator):
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("main2main-orchestrator")

    @server.list_tools()
    async def list_tools():
        return [Tool(**tool) for tool in orchestrator.get_tools()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        result = await orchestrator.handle_tool_call(name, arguments)
        return [TextContent(type="text", text=result)]

    return server
