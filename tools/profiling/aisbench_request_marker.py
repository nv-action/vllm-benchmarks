"""Mark the first AISBench request used to schedule a profiling window.

This module stays separate because importing the adapter requires the optional
AISBench package, while the rest of the profiling workflow does not.
"""

import os

from ais_bench.benchmark.models import VLLMCustomAPIChat

from tools.profiling.workflow import _mark_first_request


class ProfiledVLLMCustomAPIChat(VLLMCustomAPIChat):
    async def stream_infer(self, request_body, output):
        _mark_first_request(os.environ["ASCEND_PROFILE_REQUEST_MARKER"])
        return await super().stream_infer(request_body, output)

    async def text_infer(self, request_body, output):
        _mark_first_request(os.environ["ASCEND_PROFILE_REQUEST_MARKER"])
        return await super().text_infer(request_body, output)
