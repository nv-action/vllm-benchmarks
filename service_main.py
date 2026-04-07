from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main2main-service")


async def poll_loop(service, repo: str, interval: float, lock: asyncio.Lock, state: dict) -> None:
    while True:
        try:
            async with lock:
                result = await asyncio.to_thread(service.run_once, repo)
            state["last_poll_time"] = datetime.now(timezone.utc).isoformat()
            state["last_poll_result"] = result
            log.info("poll complete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll_loop error")
        await asyncio.sleep(interval)


async def run_mcp_sse(
    server,
    host: str,
    port: int,
    *,
    uvicorn_module=None,
    sse_transport_cls=None,
    starlette_cls=None,
    route_cls=None,
    mount_cls=None,
    response_cls=None,
) -> None:
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    uvicorn_module = uvicorn if uvicorn_module is None else uvicorn_module
    sse_transport_cls = SseServerTransport if sse_transport_cls is None else sse_transport_cls
    starlette_cls = Starlette if starlette_cls is None else starlette_cls
    route_cls = Route if route_cls is None else route_cls
    mount_cls = Mount if mount_cls is None else mount_cls
    response_cls = Response if response_cls is None else response_cls

    sse = sse_transport_cls("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )
        return response_cls()

    app = starlette_cls(
        routes=[
            route_cls("/sse", endpoint=handle_sse, methods=["GET"]),
            mount_cls("/messages/", app=sse.handle_post_message),
        ]
    )
    config = uvicorn_module.Config(app, host=host, port=port, log_level="info")
    await uvicorn_module.Server(config).serve()


async def _run(
    state_path: str,
    repo: str,
    poll_interval: float,
    mcp_host: str,
    mcp_port: int,
) -> None:
    from github_adapter import GitHubCliAdapter
    from main2main_orchestrator import Main2MainStateStore, OrchestratorService
    from mcp_server import build_mcp_server, create_mcp_protocol_server

    store = Main2MainStateStore(state_path)
    github = GitHubCliAdapter()
    service_lock = asyncio.Lock()
    poll_state: dict = {}
    service = OrchestratorService(store, github)
    orchestrator_mcp = build_mcp_server(service, store, github, service_lock, poll_state)
    mcp = create_mcp_protocol_server(orchestrator_mcp)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [task.cancel() for task in asyncio.all_tasks(loop)])

    log.info(
        "starting service: repo=%s poll_interval=%s mcp=%s:%s",
        repo,
        poll_interval,
        mcp_host,
        mcp_port,
    )

    try:
        await asyncio.gather(
            poll_loop(service, repo, poll_interval, service_lock, poll_state),
            run_mcp_sse(mcp, mcp_host, mcp_port),
        )
    except asyncio.CancelledError:
        log.info("service shutting down")


def main() -> None:
    state_path = os.environ.get("STATE_PATH", "/var/lib/vllm-benchmarks-orchestrator/state.json")
    repo = os.environ.get("REPO", "nv-action/vllm-benchmarks")
    poll_interval = float(os.environ.get("POLL_INTERVAL", "60"))
    mcp_host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp_port = int(os.environ.get("MCP_PORT", "8080"))
    asyncio.run(_run(state_path, repo, poll_interval, mcp_host, mcp_port))


if __name__ == "__main__":
    main()
