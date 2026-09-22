"""Mark the first formal AISBench request for both graphs and profiling.

This module stays separate because importing the adapter requires the optional
AISBench package, while the rest of the profiling workflow does not.
"""

import logging
import os
from pathlib import Path

from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer

from tools.profiling.capture import record_first_request


class TimedGenInferencer(GenInferencer):
    """Keep warmup outside the shared benchmark timeline."""

    async def warmup(self, *args, **kwargs):
        # AISBench routes warmup through do_request too, but does not include
        # those requests in its performance details or benchmark duration.
        self._warming_up = True
        try:
            return await super().warmup(*args, **kwargs)
        finally:
            self._warming_up = False

    def _mark_request(self) -> None:
        """Mark once per adapter; timing-only failures do not fail ordinary benchmarks."""
        if getattr(self, "_warming_up", False) or getattr(self, "_profile_marker_attempted", False):
            return
        try:
            record_first_request(Path(os.environ["ASCEND_PROFILE_REQUEST_MARKER"]))
        except (OSError, KeyError) as exc:
            if os.environ.get("ASCEND_PROFILE_ENABLED", "false").lower() == "true":
                raise
            logging.warning("Benchmark time origin unavailable: %s", exc)
        self._profile_marker_attempted = True

    async def do_request(self, data, token_bucket, session):
        """Called after request-rate/concurrency waits, before model inference."""
        self._mark_request()
        return await super().do_request(data, token_bucket, session)
