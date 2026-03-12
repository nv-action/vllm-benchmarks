from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Callable

from state_store import JsonStore

_JOBS_KEY = "_terminal_jobs"


@dataclass
class TerminalJob:
    pr_number: int
    repo: str
    terminal_reason: str
    e2e_run_id: str | None
    e2e_run_url: str | None
    fixup_run_id: str | None


class TerminalWorker:
    def __init__(
        self,
        *,
        store: JsonStore,
        extract_fn: Callable,
        summarize_fn: Callable,
        create_issue_fn: Callable,
        find_existing_issue_fn: Callable,
        update_state_fn: Callable,
        build_phase3_issue_fn: Callable,
        service_lock: asyncio.Lock | None,
    ):
        self._store = store
        self._extract_fn = extract_fn
        self._summarize_fn = summarize_fn
        self._create_issue_fn = create_issue_fn
        self._find_existing_issue_fn = find_existing_issue_fn
        self._update_state_fn = update_state_fn
        self._build_phase3_issue_fn = build_phase3_issue_fn
        self._service_lock = service_lock
        self._queue: list[dict] = []
        self._reload_pending()

    def _reload_pending(self) -> None:
        data = self._store.load()
        jobs = data.get(_JOBS_KEY, [])
        self._queue = [job for job in jobs if job.get("status") == "pending"]

    def enqueue(self, job: TerminalJob) -> None:
        entry = asdict(job)
        entry["status"] = "pending"
        with self._store.locked() as data:
            jobs = data.setdefault(_JOBS_KEY, [])
            jobs.append(entry)
        self._queue.append(entry)

    def pending_count(self) -> int:
        return len(self._queue)

    async def process_one(self) -> bool:
        if not self._queue:
            return False

        entry = self._queue[0]
        pr_number = entry["pr_number"]
        repo = entry["repo"]
        reason = entry["terminal_reason"]
        marker = (
            f"main2main-manual-review repo={repo} pr={pr_number} "
            f"fixup_run_id={entry.get('fixup_run_id')}"
        )

        existing = await asyncio.to_thread(
            self._find_existing_issue_fn,
            repo=repo,
            pr_number=pr_number,
            marker=marker,
        )
        if existing:
            await self._complete_job(entry, issue_url=existing)
            return True

        if reason == "done_failure" and entry.get("e2e_run_id"):
            analysis = await asyncio.to_thread(
                self._extract_fn,
                repo=repo,
                run_id=entry["e2e_run_id"],
            )
            body = await asyncio.to_thread(
                self._summarize_fn,
                analysis=analysis,
                terminal_reason=reason,
                pr_number=pr_number,
                e2e_run_id=entry.get("e2e_run_id"),
                e2e_run_url=entry.get("e2e_run_url"),
                fixup_run_id=entry.get("fixup_run_id"),
            )
        else:
            body = self._build_phase3_issue_fn(
                repo=repo,
                pr_number=pr_number,
                fixup_run_id=entry.get("fixup_run_id"),
                marker=marker,
            )

        issue_url = await asyncio.to_thread(
            self._create_issue_fn,
            repo=repo,
            title="main2main: manual review needed",
            body=body,
        )
        await self._complete_job(entry, issue_url=issue_url)
        return True

    async def _complete_job(self, entry: dict, *, issue_url: str) -> None:
        if self._service_lock is None:
            self._mark_done(entry, issue_url=issue_url)
            self._update_state_fn(pr_number=entry["pr_number"], status="manual_review")
            return

        async with self._service_lock:
            self._mark_done(entry, issue_url=issue_url)
            self._update_state_fn(pr_number=entry["pr_number"], status="manual_review")

    def _mark_done(self, entry: dict, *, issue_url: str) -> None:
        self._queue.remove(entry)
        with self._store.locked() as data:
            jobs = data.get(_JOBS_KEY, [])
            for job in jobs:
                if job.get("pr_number") == entry["pr_number"] and job.get("status") == "pending":
                    job["status"] = "done"
                    job["issue_url"] = issue_url
                    break
            data[_JOBS_KEY] = [job for job in jobs if job.get("status") != "done"]

    async def run_loop(self) -> None:
        while True:
            try:
                processed = await self.process_one()
                if not processed:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(10)
