import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service_main import poll_loop, run_mcp_sse


def test_poll_loop_calls_run_once_and_records_last_poll_time():
    calls = []

    class FakeService:
        def run_once(self, repo):
            calls.append(repo)
            return {"reconciled": {}}

    lock = asyncio.Lock()
    state = {}

    async def run():
        task = asyncio.create_task(poll_loop(FakeService(), "test-repo", 0.01, lock, state))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(calls) >= 1
    assert calls[0] == "test-repo"
    assert state.get("last_poll_time") is not None
    assert state.get("last_poll_result") == {"reconciled": {}}


def test_poll_loop_acquires_service_lock():
    locked_during_call = []

    class FakeService:
        def run_once(self, repo):
            return {}

    lock = asyncio.Lock()
    state = {}
    original_to_thread = asyncio.to_thread

    async def patched_poll_loop(service, repo, interval, lk, st):
        async with lk:
            locked_during_call.append(True)
            result = await original_to_thread(service.run_once, repo)
        st["last_poll_result"] = result

    asyncio.run(patched_poll_loop(FakeService(), "r", 0.01, lock, state))
    assert locked_during_call == [True]


def test_run_mcp_sse_wraps_low_level_server_with_sse_transport():
    calls = []

    class FakeMcpServer:
        def create_initialization_options(self):
            return "init-options"

        async def run(self, read_stream, write_stream, initialization_options):
            calls.append((read_stream, write_stream, initialization_options))

    class FakeTransport:
        handle_post_message = object()

        def __init__(self, endpoint):
            assert endpoint == "/messages/"

        @asynccontextmanager
        async def connect_sse(self, scope, receive, send):
            yield ("read-stream", "write-stream")

    class FakeRoute:
        def __init__(self, path, endpoint, methods):
            self.path = path
            self.endpoint = endpoint
            self.methods = methods

    class FakeMount:
        def __init__(self, path, app):
            self.path = path
            self.app = app

    class FakeResponse:
        pass

    class FakeRequest:
        scope = {"type": "http"}

        async def receive(self):
            return {}

        async def _send(self, message):
            return None

    class FakeStarlette:
        def __init__(self, routes):
            self.routes = routes

    class FakeConfig:
        def __init__(self, app, host, port, log_level):
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class FakeUvicornServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            sse_route = next(route for route in self.config.app.routes if getattr(route, "path", None) == "/sse")
            response = await sse_route.endpoint(FakeRequest())
            assert isinstance(response, FakeResponse)

    class FakeUvicornModule:
        Config = FakeConfig
        Server = FakeUvicornServer

    asyncio.run(
        run_mcp_sse(
            FakeMcpServer(),
            "127.0.0.1",
            8080,
            uvicorn_module=FakeUvicornModule,
            sse_transport_cls=FakeTransport,
            starlette_cls=FakeStarlette,
            route_cls=FakeRoute,
            mount_cls=FakeMount,
            response_cls=FakeResponse,
        )
    )

    assert calls == [("read-stream", "write-stream", "init-options")]
