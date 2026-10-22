"""Stateless Responses adapter with per-attempt reservations and claim/deadline gates.

Pinned openai 3.36.1 exposes ``client.responses.input_tokens.count``; counting uses that
SDK method (POST /v1/responses/input_tokens). Clients inject legacy httpx.AsyncClient
and disable SDK retries. The application startup gate verifies counting is unbilled.
"""

from __future__ import annotations

import asyncio
import copy
import time
from decimal import Decimal

import openai
from openai.types.responses import ResponseOutputMessage
from sqlalchemy import text

from support_api.agent.models.base import (
    FunctionCall,
    ModelContext,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from support_api.db import Database
from support_api.turns.claim import ClaimLost, DeadlineExpired, fenced, pre_send
from support_api.usage.budget import (
    BudgetExceeded,
    Usage,
    actual_usd,
    mark_uncertain,
    release,
    reserve,
    settle,
    worst_case_usd,
)
from support_api.usage.classify import (
    Decision,
    OutcomeClass,
    classify_error,
    classify_response,
    rate_limit_delay,
)
from support_api.usage.pricing import Pricing, Rate

# The counting endpoint rejects these generation-only fields.
_GENERATION_ONLY = frozenset({"max_output_tokens", "store", "service_tier", "include"})
_BLOCK_UNKNOWN = text(
    "UPDATE support.conversations SET live_blocked_reason = 'provider_outcome_unknown' "
    "WHERE id = :c AND live_blocked_reason IS NULL"
)


class OpenAIResponsesModel:
    live = True

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        db: Database,
        model: str,
        tier: str,
        rate: Rate,
        max_output_tokens: int,
        count_timeout_s: float = 10,
        attempt_timeout_s: float = 20,
        conversation_cap_usd: Decimal = Decimal("0.10"),
        pricing: Pricing | None = None,
    ) -> None:
        if client.max_retries != 0:
            raise ValueError("OpenAIResponsesModel requires SDK max_retries=0")
        self.name = model
        self._client = client
        self._db = db
        self._tier = tier
        self._rate = rate
        self._max_output_tokens = max_output_tokens
        self._count_timeout_s = count_timeout_s
        self._attempt_timeout_s = attempt_timeout_s
        self._conversation_cap_usd = conversation_cap_usd
        self._pricing = pricing

    async def aclose(self) -> None:
        await self._client.close()

    def build_generation_body(self, req: ModelRequest) -> dict:
        return {
            "model": self.name,
            "instructions": req.instructions,
            "input": list(req.input),
            "tools": list(req.tools),
            "tool_choice": "auto",
            "max_output_tokens": self._max_output_tokens,
            "store": False,
            "service_tier": self._tier,
            "include": ["reasoning.encrypted_content"],
        }

    def build_count_body(self, req: ModelRequest) -> dict:
        return {
            key: value
            for key, value in self.build_generation_body(req).items()
            if key not in _GENERATION_ONLY
        }

    async def count_input_tokens(self, request: ModelRequest, ctx: ModelContext) -> int:
        ctx.counted_request = ctx.input_tokens = ctx.counted_model = None
        counted_request = copy.deepcopy(request)
        body = self.build_count_body(counted_request)
        try:
            await pre_send(self._db, ctx.claim, ctx.deadline, ctx.checkpoints)
            timeout = ctx.deadline.timeout(self._count_timeout_s)
            async with asyncio.timeout(timeout):
                result = await self._client.responses.input_tokens.count(
                    **body, timeout=timeout
                )
            tokens = result.input_tokens
            if type(tokens) is not int or tokens < 0:
                raise ValueError("invalid input token count")
        except ClaimLost:
            raise
        except DeadlineExpired as exc:
            raise ModelUnavailable("limit_reached") from exc
        except Exception as exc:
            ctx.trace.append({
                "kind": "token_count", "model": self.name, "service_tier": None,
                "input_tokens": None, "output_tokens": None,
                "estimated_cost_usd": None, "outcome_class": "count_failed",
            })
            raise ModelUnavailable("upstream_unavailable") from exc
        ctx.trace.append({
            "kind": "token_count", "model": self.name, "service_tier": None,
            "input_tokens": tokens, "output_tokens": None,
            "estimated_cost_usd": None, "outcome_class": "completed",
        })
        ctx.counted_request = counted_request
        ctx.input_tokens = tokens
        ctx.counted_model = self.name
        return tokens

    def _response_rate(self, tier: str | None) -> Rate | None:
        if not isinstance(tier, str):
            return None
        if self._pricing is not None:
            return self._pricing.rate(self.name, tier)
        return self._rate if tier == self._tier else None

    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse:
        if (
            ctx.counted_request != request
            or ctx.input_tokens is None
            or ctx.counted_model != self.name
        ):
            raise ModelUnavailable("upstream_unavailable")
        amount = worst_case_usd(ctx.input_tokens, self._max_output_tokens, self._rate)
        body = self.build_generation_body(request)
        for attempt in (1, 2):
            try:
                async with fenced(self._db, ctx.claim) as conn:
                    call_id = await reserve(
                        conn,
                        ctx.claim,
                        amount=amount,
                        kind="generation",
                        model=self.name,
                        tier=self._tier,
                        attempt=attempt,
                        conversation_cap_usd=self._conversation_cap_usd,
                    )
            except BudgetExceeded as exc:
                raise ModelUnavailable("live_blocked") from exc
            await ctx.checkpoints.hit("after_reserve")
            try:
                await pre_send(self._db, ctx.claim, ctx.deadline, ctx.checkpoints)
            except DeadlineExpired as exc:
                async with fenced(self._db, ctx.claim) as conn:
                    await release(
                        conn, ctx.claim, call_id, outcome_class="deadline_expired"
                    )
                raise ModelUnavailable("limit_reached") from exc
            # ClaimLost propagates, keeping the full reservation for recovery.
            started = time.monotonic()
            usage = None
            rate = None
            response = None
            tier = None
            retry_after = None
            request_id = None
            provider_error = None
            try:
                timeout = ctx.deadline.timeout(self._attempt_timeout_s)
                async with asyncio.timeout(timeout):
                    response = await self._client.responses.create(
                        **body, timeout=timeout
                    )
            except Exception as exc:  # noqa: BLE001 - every provider failure needs a ledger decision
                provider_error = exc
            latency = int((time.monotonic() - started) * 1000)
            await ctx.checkpoints.hit("after_send")
            if provider_error is not None:
                decision, request_id, retry_after = _classify_exception(provider_error)
            else:
                usage = _reported_usage(response)
                tier = _text_or_none(getattr(response, "service_tier", None))
                rate = self._response_rate(tier)
                request_id = getattr(response, "_request_id", None)
                decision = classify_response(
                    has_usage=usage is not None, tier_priced=rate is not None
                )
            metadata = {"latency_ms": latency, "provider_request_id": request_id}
            if decision.ledger == "settle":
                # The only unfenced ledger write; usage belongs to this worker's own call.
                await settle(
                    self._db,
                    call_id,
                    usage,
                    rate,
                    outcome_class=decision.outcome,
                    service_tier=tier,
                    **metadata,
                )
            else:
                async with fenced(self._db, ctx.claim) as conn:
                    if decision.ledger == "release":
                        await release(
                            conn,
                            ctx.claim,
                            call_id,
                            outcome_class=decision.outcome,
                            **metadata,
                        )
                    else:
                        await mark_uncertain(
                            conn,
                            ctx.claim,
                            call_id,
                            decision.outcome,
                            decision.block_reason,
                            service_tier=tier,
                            **metadata,
                        )
            ctx.trace.append(
                {
                    "kind": "generation",
                    "model": self.name,
                    "attempt": attempt,
                    "status": decision.ledger,
                    "error_code": decision.block_reason,
                    "service_tier": tier,
                    "input_tokens": usage.input_tokens if usage is not None else None,
                    "output_tokens": usage.output_tokens if usage is not None else None,
                    "estimated_cost_usd": str(actual_usd(usage, rate))
                    if usage is not None and rate is not None else None,
                    "outcome_class": str(decision.outcome),
                }
            )
            if decision.ledger == "settle":
                try:
                    return _model_response(response)
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    # Usage is known even when the answer is malformed. Keep settlement,
                    # but stop live continuation under the worker's current fencing token.
                    async with fenced(self._db, ctx.claim) as conn:
                        await conn.execute(
                            _BLOCK_UNKNOWN, {"c": ctx.claim.conversation_id}
                        )
                    ctx.trace[-1]["error_code"] = "provider_outcome_unknown"
                    raise ModelUnavailable("upstream_unavailable") from exc
            if not decision.retryable or attempt == 2:
                break
            if decision.outcome == OutcomeClass.RATE_LIMITED:
                delay = rate_limit_delay(retry_after, ctx.deadline.remaining())
                if delay is None:
                    break
                await asyncio.sleep(delay)
            # A fresh reservation and pre-send gate check ownership after any backoff.
        raise ModelUnavailable("upstream_unavailable")


def _text_or_none(value) -> str | None:
    return value if isinstance(value, str) else None


def _classify_exception(exc: Exception) -> tuple[Decision, str | None, str | None]:
    """Keep the original exception chain and distrust malformed HTTP error fields."""
    status = error_type = error_code = request_id = retry_after = None
    if isinstance(exc, openai.APIStatusError):
        status = exc.response.status_code
        request_id = exc.response.headers.get("x-request-id")
        retry_after = exc.response.headers.get("Retry-After")
        try:
            error = exc.response.json().get("error", {})
            if isinstance(error, dict):
                error_type = _text_or_none(error.get("type"))
                error_code = _text_or_none(error.get("code"))
        except (ValueError, AttributeError):
            pass
    decision = classify_error(
        exc=exc, status=status, error_type=error_type, error_code=error_code
    )
    return decision, request_id, retry_after


def _reported_usage(response) -> Usage | None:
    """Accept only actual, complete nonnegative usage; never synthesize a zero usage record."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    counts = (
        getattr(usage, "input_tokens", None),
        getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None),
        getattr(usage, "output_tokens", None),
    )
    if (
        any(type(n) is not int or n < 0 for n in counts)
        or counts[1] > counts[0]
        or counts[0] + counts[2] == 0
    ):
        return None
    return Usage(*counts)


def _model_response(response) -> ModelResponse:
    if response.status not in ("completed", "incomplete") or not isinstance(
        response.output, list
    ):
        raise ValueError("invalid provider response")
    items = tuple(
        item.model_dump(mode="json", exclude_unset=True, warnings="error")
        for item in response.output
    )
    calls = []
    texts = []
    for item in items:
        if item.get("type") == "function_call":
            if not all(
                isinstance(item.get(k), str) and item[k]
                for k in ("call_id", "name", "arguments")
            ):
                raise ValueError("invalid provider function call")
            calls.append(FunctionCall(item["call_id"], item["name"], item["arguments"]))
        elif item.get("type") == "message":
            # SDK HTTP parsing constructs models without validating required fields.
            # Validate its pinned message schema while retaining the original output items.
            message = ResponseOutputMessage.model_validate(item, strict=True)
            for part in message.content:
                if part.type == "output_text":
                    texts.append(part.text)
    return ModelResponse(response.status, "".join(texts) or None, tuple(calls), items)
