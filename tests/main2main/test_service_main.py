import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service_main import poll_loop


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
