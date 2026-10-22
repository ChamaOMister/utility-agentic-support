"""AgentCore: the bounded agent loop for one turn (spec §5.4, §5.4.1).

Transcript ownership: AgentCore writes only the ``reasoning`` and ``function_call`` items of a
tool-calling response (one fenced transaction, before any call runs) and each
``function_call_output`` (fenced, right after its tool runs). It never writes an
``assistant_message``: the final response is returned to the turn service, which stores it.

Every ending with a catalog message is ``failed``; a model's final text is ``completed``.
``ClaimLost`` and ``BaseException`` propagate; any other unexpected error ends the turn with
``upstream_unavailable``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.agent.history import (
    HistoryInvariantError,
    Item,
    TurnItems,
    check_tool_pairs,
    estimate_tokens,
    load_past_turns,
    select_turns,
    to_request_input,
)
from support_api.agent.models.base import (
    ChatModel,
    ModelContext,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from support_api.catalog import MESSAGES
from support_api.checkpoints import Checkpoints
from support_api.db import Database
from support_api.deadline import Deadline
from support_api.settings import Settings
from support_api.tools.executor import Executor, to_model_output
from support_api.tools.schemas import ToolContext, ToolTrace
from support_api.turns.claim import Claim, ClaimLost, DeadlineExpired, fenced
from support_api.usage.pricing import ReasoningMode

logger = logging.getLogger("support_api.agent")

_SERVICE_DIR = Path(__file__).resolve().parents[3]
_PERSISTED_KINDS = frozenset({"reasoning", "function_call"})

_LOAD_CURRENT = text(
    "SELECT turn_id, seq, kind, call_id, model, payload FROM support.conversation_items "
    "WHERE turn_id = :t ORDER BY seq"
)
_LIVE_BLOCKED = text(
    "SELECT live_blocked_reason FROM support.conversations WHERE id = :c"
)
_NEXT_SEQ = text(
    "SELECT COALESCE(MAX(seq), 0) + 1 FROM support.conversation_items WHERE turn_id = :t"
)
_INSERT_ITEM = text(
    "INSERT INTO support.conversation_items "
    "(conversation_id, turn_id, seq, kind, call_id, model, payload) "
    "VALUES (:c, :t, :q, :k, :i, :m, CAST(:p AS jsonb))"
)


@dataclass
class TurnOutcome:
    status: Literal["completed", "failed"]
    reply: str
    trace: dict  # {"tool_calls": [...], "model_calls": [...]}
    final_items: tuple[
        dict, ...
    ]  # final response's non-message items; () for catalog endings


class _End(Exception):
    """Ends the turn with a catalog message (always ``failed``)."""

    def __init__(self, key: str, error_code: str | None = None) -> None:
        super().__init__(key)
        self.key = key
        self.error_code = error_code or key


def load_instructions(path: Path) -> str:
    """Read the system prompt; a relative path falls back to the service directory."""
    if not path.is_absolute() and not path.exists():
        path = _SERVICE_DIR / path
    return path.read_text(encoding="utf-8")


def _instructions_estimate(instructions: str) -> int:
    # Same rule as the scripted/fake local count (estimate_request_tokens).
    return len(instructions) // 3


def _has_nul(value: object) -> bool:
    """True when any string in ``value`` holds NUL, which a jsonb payload cannot store."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


class AgentCore:
    def __init__(
        self,
        db: Database,
        model: ChatModel,
        executor: Executor,
        settings: Settings,
        checkpoints: Checkpoints = Checkpoints(),  # noqa: B008 - stateless default hooks
        *,
        reasoning_mode: ReasoningMode = "current_turn",
    ) -> None:
        self._db = db
        self._model = model
        self._executor = executor
        self._settings = settings
        self._checkpoints = checkpoints
        self._reasoning_mode = reasoning_mode
        self._instructions = load_instructions(settings.system_prompt_path)
        self._tools = tuple(executor.tool_definitions())

    @property
    def model_name(self) -> str:
        """The model's name, stored with the reasoning items of a final response."""
        return self._model.name

    async def handle_turn(
        self,
        claim: Claim,
        text: str,
        channel: Literal["chat"],
        deadline: Deadline,
    ) -> TurnOutcome:
        ctx = ModelContext(
            claim=claim, deadline=deadline, checkpoints=self._checkpoints, trace=[]
        )
        tool_traces: list[ToolTrace] = []
        try:
            return await self._run(claim, text, ctx, tool_traces)
        except ClaimLost:
            raise
        except _End as end:
            key, error_code = end.key, end.error_code
        except DeadlineExpired:
            key, error_code = "limit_reached", "limit_reached"
        except ModelUnavailable as e:
            key, error_code = e.catalog_key, e.catalog_key
        except HistoryInvariantError:
            key, error_code = "limit_reached", "internal_error"
        except Exception:  # noqa: BLE001 - never log the text: it may quote customer data
            key, error_code = "upstream_unavailable", "internal_error"
        log = logger.error if error_code == "internal_error" else logger.warning
        log(
            "agent_turn_failed",
            extra={
                "conversation_id": str(claim.conversation_id),
                "turn_id": str(claim.turn_id),
                "claim_version": claim.claim_version,
                "error_code": error_code,
            },
        )
        return TurnOutcome(
            status="failed",
            reply=MESSAGES[key],
            trace=self._trace(tool_traces, ctx),
            final_items=(),
        )

    # -- the loop ---------------------------------------------------------------------------
    async def _run(
        self,
        claim: Claim,
        user_text: str,
        ctx: ModelContext,
        tool_traces: list[ToolTrace],
    ) -> TurnOutcome:
        async with self._db.begin() as conn:
            blocked = (
                await conn.execute(_LIVE_BLOCKED, {"c": claim.conversation_id})
            ).scalar_one_or_none()
            past = await load_past_turns(conn, claim.conversation_id, claim.turn_id)
            current = await self._load_current(conn, claim, user_text)
        if blocked is not None and self._model.live:
            raise _End("live_blocked")

        s = self._settings
        model_calls = 0
        tools_run = 0
        while True:
            if ctx.deadline.expired:
                raise DeadlineExpired
            if model_calls >= s.max_model_calls:
                raise _End("limit_reached")
            request = await self._prepare(past, current, ctx)
            model_calls += 1
            response = await self._tracked(
                "generation", self._model.generate, request, ctx
            )
            await self._checkpoints.hit("after_model_response")

            if response.status != "completed":
                raise _End("limit_reached")  # a truncated answer is never shown or run
            if not response.function_calls:
                # Postgres text/jsonb cannot hold NUL: strip it so storing the reply cannot fail.
                reply = (response.text or "").replace("\x00", "")
                if not reply:
                    raise _End("limit_reached")
                # Only the latest tool result decides: re-verifying later in the turn clears it.
                if tool_traces and tool_traces[-1].error_code == "verification_expired":
                    raise _End("verification_expired")
                return TurnOutcome(
                    status="completed",
                    reply=reply,
                    trace=self._trace(tool_traces, ctx),
                    final_items=tuple(
                        i
                        for i in response.output_items
                        if i.get("type") != "message" and not _has_nul(i)
                    ),
                )

            calls = response.function_calls
            if tools_run + len(calls) > s.max_tool_calls:
                raise _End("limit_reached")  # nothing of this response is stored or run
            new_items = self._call_items(response)
            check_tool_pairs(
                [*request.input, *new_items],
                pending_call_ids=frozenset(c.call_id for c in calls),
            )
            current += await self._persist(claim, new_items, self._model.name)

            for fc in calls:
                if ctx.deadline.expired:
                    raise DeadlineExpired
                result, trace = await self._executor.run(
                    fc.name,
                    fc.arguments,
                    ToolContext(claim, ctx.deadline, self._checkpoints),
                )
                tools_run += 1
                tool_traces.append(trace)
                output = {
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": to_model_output(result),
                }
                current += await self._persist(claim, [output], None)

    async def _load_current(
        self, conn: AsyncConnection, claim: Claim, user_text: str
    ) -> list[Item]:
        rows = (await conn.execute(_LOAD_CURRENT, {"t": claim.turn_id})).all()
        items = [
            Item(r.turn_id, r.seq, r.kind, r.call_id, r.model, r.payload) for r in rows
        ]
        if not any(i.kind == "user_message" for i in items):
            # The turn service writes the user item first; this keeps the request well formed
            # if it has not (nothing is persisted here: user_message has a single writer).
            user = {"type": "message", "role": "user", "content": user_text}
            items.insert(0, Item(claim.turn_id, 0, "user_message", None, None, user))
        return items

    async def _prepare(
        self, past: list[TurnItems], current: list[Item], ctx: ModelContext
    ) -> ModelRequest:
        """Select history, check the pairing invariant and count; drop the oldest turn once."""
        limit = self._settings.max_input_tokens
        budget = (
            limit
            - sum(estimate_tokens(i) for i in current)
            - _instructions_estimate(self._instructions)
        )
        if budget < 0:
            raise _End("limit_reached")  # the current turn alone cannot fit
        selected = select_turns(
            past,
            max_turns=self._settings.history_max_turns,
            token_budget=budget,
            estimate=estimate_tokens,
        )
        request = self._build(selected, current)
        if (
            await self._tracked(
                "token_count", self._model.count_input_tokens, request, ctx
            )
            <= limit
        ):
            return request
        if not selected:
            raise _End("limit_reached")
        request = self._build(selected[1:], current)
        if (
            await self._tracked(
                "token_count", self._model.count_input_tokens, request, ctx
            )
            <= limit
        ):
            return request
        raise _End("limit_reached")

    def _build(self, turns: list[TurnItems], current: list[Item]) -> ModelRequest:
        items = to_request_input(
            turns, current, mode=self._reasoning_mode, model=self._model.name
        )
        check_tool_pairs(
            items
        )  # before every count and generation; raises before sending
        return ModelRequest(
            instructions=self._instructions, input=tuple(items), tools=self._tools
        )

    @staticmethod
    def _call_items(response: ModelResponse) -> list[dict]:
        """The response's reasoning and function_call items; interim messages are dropped."""
        items = [i for i in response.output_items if i.get("type") in _PERSISTED_KINDS]
        stored_ids = [
            i.get("call_id") for i in items if i.get("type") == "function_call"
        ]
        if stored_ids != [c.call_id for c in response.function_calls]:
            raise HistoryInvariantError("function calls do not match the output items")
        return items

    async def _persist(
        self, claim: Claim, payloads: list[dict], model: str | None
    ) -> list[Item]:
        """Append items to the current turn in one fenced transaction."""
        async with fenced(self._db, claim) as conn:
            seq = (await conn.execute(_NEXT_SEQ, {"t": claim.turn_id})).scalar_one()
            stored: list[Item] = []
            for offset, payload in enumerate(payloads):
                kind = payload["type"]
                call_id = payload.get("call_id") if kind != "reasoning" else None
                await conn.execute(
                    _INSERT_ITEM,
                    {
                        "c": claim.conversation_id,
                        "t": claim.turn_id,
                        "q": seq + offset,
                        "k": kind,
                        "i": call_id,
                        "m": model,
                        "p": json.dumps(payload, ensure_ascii=False),
                    },
                )
                stored.append(
                    Item(claim.turn_id, seq + offset, kind, call_id, model, payload)
                )
        return stored

    async def _tracked[T](
        self,
        kind: Literal["token_count", "generation"],
        fn: Callable[[ModelRequest, ModelContext], Awaitable[T]],
        request: ModelRequest,
        ctx: ModelContext,
    ) -> T:
        """Run one model call; add a minimal trace entry unless the model recorded its own."""
        before = len(ctx.trace)
        status = "error"
        try:
            result = await fn(request, ctx)
            status = "ok"
            return result
        finally:
            if len(ctx.trace) == before:
                ctx.trace.append(
                    {"kind": kind, "model": self._model.name, "status": status}
                )

    @staticmethod
    def _trace(tool_traces: list[ToolTrace], ctx: ModelContext) -> dict:
        return {
            "tool_calls": [asdict(t) for t in tool_traces],
            "model_calls": list(ctx.trace),
        }
