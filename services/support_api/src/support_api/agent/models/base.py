"""The ChatModel protocol and the request/response shapes shared by every model (spec §5.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from support_api.checkpoints import Checkpoints
from support_api.deadline import Deadline
from support_api.turns.claim import Claim


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str  # JSON text, exactly as the model produced it


@dataclass(frozen=True)
class ModelRequest:
    instructions: str
    input: tuple[dict, ...]
    tools: tuple[dict, ...]


@dataclass(frozen=True)
class ModelResponse:
    status: Literal["completed", "incomplete"]
    text: str | None
    function_calls: tuple[FunctionCall, ...]
    output_items: tuple[dict, ...]  # all items incl. reasoning, for persistence


@dataclass
class ModelContext:
    claim: Claim
    deadline: Deadline
    checkpoints: Checkpoints
    trace: list[dict]


class ModelUnavailable(Exception):
    """The model cannot answer; ``catalog_key`` selects the customer-facing message."""

    def __init__(
        self,
        catalog_key: Literal["upstream_unavailable", "live_blocked", "limit_reached"],
    ) -> None:
        super().__init__(catalog_key)
        self.catalog_key = catalog_key


class ChatModel(Protocol):
    name: str

    async def count_input_tokens(
        self, request: ModelRequest, ctx: ModelContext
    ) -> int: ...

    async def generate(
        self, request: ModelRequest, ctx: ModelContext
    ) -> ModelResponse: ...
