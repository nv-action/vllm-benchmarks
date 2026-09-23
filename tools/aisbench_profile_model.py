"""AISBench model type resolved after MMEngine parses the request config."""

import os

from ais_bench.benchmark.models import VLLMCustomAPIChat

from tools.profile import _mark_first_request


class ProfiledVLLMCustomAPIChat(VLLMCustomAPIChat):
    async def stream_infer(self, request_body, output):
        _mark_first_request(os.environ["NIGHTLY_PROFILE_REQUEST_MARKER"])
        return await super().stream_infer(request_body, output)

    async def text_infer(self, request_body, output):
        _mark_first_request(os.environ["NIGHTLY_PROFILE_REQUEST_MARKER"])
        return await super().text_infer(request_body, output)
