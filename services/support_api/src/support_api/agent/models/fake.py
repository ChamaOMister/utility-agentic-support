"""A queued-response fake for edge-case tests: returns (or raises) what the test scripted."""

from __future__ import annotations

from support_api.agent.models.base import ModelContext, ModelRequest, ModelResponse
from support_api.agent.models.scripted import estimate_request_tokens


class QueueExhausted(AssertionError):
    """The fake was asked for more responses than the test queued."""


class QueuedFakeModel:
    name = "queued-fake"
    live = False

    def __init__(
        self,
        responses: list[ModelResponse | Exception],
        *,
        token_count: int | None = None,
        live: bool = False,
    ) -> None:
        self.live = live
        self._queue = list(responses)
        self._token_count = token_count
        self.requests: list[ModelRequest] = []
        self.count_requests: list[ModelRequest] = []

    def push(self, *responses: ModelResponse | Exception) -> None:
        """Queue more responses (tests that drive several turns through one model)."""
        self._queue.extend(responses)

    async def count_input_tokens(self, request: ModelRequest, ctx: ModelContext) -> int:
        self.count_requests.append(request)
        if self._token_count is not None:
            return self._token_count
        return estimate_request_tokens(request)

    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse:
        self.requests.append(request)
        if not self._queue:
            raise QueueExhausted(
                f"QueuedFakeModel has no queued response left (request #{len(self.requests)})"
            )
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
