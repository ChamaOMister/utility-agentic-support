"""Tool executor (spec §5.3): parse -> assurance -> handler -> typed result, one trace per run.

Fencing: ``ClaimLost`` always propagates, never converted to a tool error. Every billing call runs
behind the pre-send gate (``pre_send``), so a lost claim stops the request before it is sent.
Logs carry allowlisted metadata only; arguments, identifiers, dates and customer data never reach
a log line.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from support_api.db import Database
from support_api.identity.assurance import current_assurance
from support_api.identity.attempts import (
    DEFAULT_LIMITS,
    AdmissionRejected,
    IdentityLimits,
    admit,
    apply_verified,
    record_outcome,
)
from support_api.identity.normalize import lockout_key, normalize
from support_api.integrations.billing.client import BillingClient, BillingError, Guard
from support_api.turns.claim import ClaimLost, DeadlineExpired, fenced, pre_send

from .schemas import (
    SPEC_BY_NAME,
    TOOL_SPECS,
    AuthenticateArgs,
    GetBillArgs,
    ListBillsArgs,
    ToolContext,
    ToolError,
    ToolResult,
    ToolTrace,
)

logger = logging.getLogger("support_api.tools")

UNKNOWN_TOOL_NAME = "unknown"

_INSERT_TOOL_CALL = text(
    "INSERT INTO support.tool_calls "
    "(turn_id, name, status, error_code, attempts, duration_ms, record_count) "
    "VALUES (:t, :n, :s, :e, :a, :d, :r)"
)


@dataclass
class _Run:
    """Mutable per-run facts the trace needs but a handler's return value cannot carry."""

    attempts: int = 0
    trace_error_code: str | None = None  # overrides the ToolError code in the trace


ToolOutcome = ToolResult | ToolError


class Executor:
    def __init__(
        self,
        db: Database,
        billing: BillingClient,
        identifier_hash_key: bytes,
        *,
        limits: IdentityLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not identifier_hash_key:
            raise ValueError("identifier_hash_key must not be empty")
        self._db = db
        self._billing = billing
        self._key = identifier_hash_key
        self._limits = limits or DEFAULT_LIMITS
        self._clock = clock

    def tool_definitions(self) -> list[dict]:
        """Responses API function tools, generated from the argument models."""
        definitions = []
        for spec in TOOL_SPECS:
            parameters = spec.args.model_json_schema()
            parameters.pop("title", None)
            for prop in parameters.get("properties", {}).values():
                prop.pop("title", None)
            parameters["additionalProperties"] = False
            definitions.append(
                {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": parameters,
                    "strict": False,
                }
            )
        return definitions

    async def run(
        self, name: str, raw_arguments: str, ctx: ToolContext
    ) -> tuple[ToolOutcome, ToolTrace]:
        started = self._clock()
        state = _Run()
        try:
            result = await self._execute(name, raw_arguments, ctx, state)
        except ClaimLost:
            raise
        except Exception:  # noqa: BLE001 - any handler failure becomes internal_error
            # Never log the exception text or traceback: it may quote customer data.
            result = ToolError(code="internal_error")
        # Unknown names are model-supplied free text (possibly customer data): never
        # let them into trace metadata, the tool_calls row or the log (spec 5.7).
        trace_name = name if name in SPEC_BY_NAME else UNKNOWN_TOOL_NAME
        trace = self._trace(trace_name, result, state, started)
        await self._persist(ctx, trace)  # ClaimLost here propagates
        self._log(trace)
        return result, trace

    # -- pipeline ---------------------------------------------------------------------------
    async def _execute(
        self, name: str, raw_arguments: str, ctx: ToolContext, state: _Run
    ) -> ToolOutcome:
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            return ToolError(code="invalid_arguments")
        try:
            args = spec.args.model_validate_json(raw_arguments)
        except ValidationError:
            return ToolError(code="invalid_arguments")

        if isinstance(args, AuthenticateArgs):
            return await self._authenticate(args, ctx, state)

        async with self._db.begin() as conn:
            assurance = await current_assurance(conn, ctx.claim.conversation_id)
        if assurance.level == "none" or assurance.customer_ref is None:
            if assurance.expired:
                state.trace_error_code = "verification_expired"
            return ToolError(code="authentication_required")
        ref = assurance.customer_ref  # the only source of customer_ref
        guard = self._guard(ctx)

        if isinstance(args, ListBillsArgs):
            return await self._billing_call(
                state,
                lambda: self._billing.list_bills(
                    ref,
                    status=args.status,
                    limit=args.limit,
                    deadline=ctx.deadline,
                    guard=guard,
                ),
                lambda bills: ToolResult(
                    data=[b.model_dump(mode="json") for b in bills],
                    record_count=len(bills),
                ),
            )
        if isinstance(args, GetBillArgs):
            return await self._billing_call(
                state,
                lambda: self._billing.get_bill(
                    ref, args.bill_id, deadline=ctx.deadline, guard=guard
                ),
                lambda bill: ToolResult(
                    data=bill.model_dump(mode="json"), record_count=1
                ),
            )
        return await self._billing_call(  # get_account_summary
            state,
            lambda: self._billing.get_account(ref, deadline=ctx.deadline, guard=guard),
            lambda account: ToolResult(
                data={
                    "installations": [
                        i.model_dump(mode="json") for i in account.installations
                    ],
                    "tariff_class": account.tariff_class,
                    "account_status": account.account_status,
                    # Street (and state) deliberately never reach the model.
                    "address": {
                        "neighborhood": account.address.neighborhood,
                        "city": account.address.city,
                    },
                },
                record_count=1,
            ),
        )

    def _guard(self, ctx: ToolContext) -> Guard:
        async def guard() -> None:
            await pre_send(self._db, ctx.claim, ctx.deadline, ctx.checkpoints)

        return guard

    async def _billing_call[T](
        self,
        state: _Run,
        call: Callable[[], Awaitable[Any]],
        to_result: Callable[[Any], ToolResult],
    ) -> ToolOutcome:
        try:
            result = await call()
        except BillingError as e:
            state.attempts = e.attempts
            return ToolError(code=e.code)
        except DeadlineExpired:
            return ToolError(code="upstream_timeout")
        state.attempts = result.attempts
        return to_result(result.value)

    # -- authenticate_customer (§5.2) -------------------------------------------------------
    async def _authenticate(
        self, args: AuthenticateArgs, ctx: ToolContext, state: _Run
    ) -> ToolOutcome:
        normalized = normalize(args.identifier)
        if normalized is None:
            return ToolError(code="invalid_arguments")
        key = lockout_key(normalized, self._key)

        try:
            async with fenced(self._db, ctx.claim) as conn:
                # A rejection propagates out of the block, so the transaction rolls back and
                # nothing from the rejected admission is committed.
                attempt_id = await admit(conn, ctx.claim, key, limits=self._limits)
        except AdmissionRejected as rejected:
            return ToolError(code=rejected.code)

        try:
            await ctx.checkpoints.hit("after_verify_admit")
            result = await self._billing.verify(
                normalized.digits,  # normalized digits, never the raw input
                args.birth_date,
                args.phone_last4,
                deadline=ctx.deadline,
                guard=self._guard(ctx),
            )
        except ClaimLost:
            raise  # nothing sent; no outcome recorded: recovery abandons the slot
        except DeadlineExpired:
            await record_outcome(
                self._db, attempt_id, "upstream_error", limits=self._limits
            )
            return ToolError(code="upstream_timeout")
        except BillingError as e:
            state.attempts = e.attempts
            await record_outcome(
                self._db, attempt_id, "upstream_error", limits=self._limits
            )
            return ToolError(code=e.code)
        except Exception:
            # Unknown failure before any answer: free the slot without counting, then surface it.
            with suppress(Exception):
                await record_outcome(
                    self._db, attempt_id, "upstream_error", limits=self._limits
                )
            raise

        state.attempts = result.attempts
        verdict = result.value
        if not verdict.verified:
            await record_outcome(self._db, attempt_id, "failed", limits=self._limits)
            return ToolError(code="authentication_failed")
        if verdict.customer_ref is None or verdict.first_name is None:
            # Contract violation: a "verified" answer without who was verified.
            await record_outcome(
                self._db, attempt_id, "upstream_error", limits=self._limits
            )
            return ToolError(code="upstream_unavailable")
        if not await apply_verified(
            self._db,
            ctx.claim,
            attempt_id,
            verdict.customer_ref,
            verdict.first_name,
            limits=self._limits,
        ):
            raise ClaimLost(
                f"claim lost while applying verification (turn {ctx.claim.turn_id})"
            )
        return ToolResult(data={"first_name": verdict.first_name}, record_count=1)

    # -- trace, persistence, logging ----------------------------------------------------------
    def _trace(
        self, name: str, result: ToolOutcome, state: _Run, started: float
    ) -> ToolTrace:
        duration_ms = max(0, round((self._clock() - started) * 1000))
        if isinstance(result, ToolError):
            return ToolTrace(
                name, "error", state.trace_error_code or result.code,
                state.attempts, duration_ms, 0,
            )  # fmt: skip
        return ToolTrace(
            name, "ok", None, state.attempts, duration_ms, result.record_count
        )

    async def _persist(self, ctx: ToolContext, trace: ToolTrace) -> None:
        async with fenced(self._db, ctx.claim) as conn:
            await conn.execute(
                _INSERT_TOOL_CALL,
                {
                    "t": ctx.claim.turn_id,
                    "n": trace.name,
                    "s": trace.status,
                    "e": trace.error_code,
                    "a": trace.attempts,
                    "d": trace.duration_ms,
                    "r": trace.record_count,
                },
            )

    def _log(self, trace: ToolTrace) -> None:
        extra: dict[str, Any] = {
            "tool_name": trace.name,
            "status": trace.status,
            "attempt": trace.attempts,
            "duration_ms": trace.duration_ms,
            "record_count": trace.record_count,
        }
        if trace.error_code is not None:
            extra["error_code"] = trace.error_code
        logger.log(
            logging.INFO if trace.status == "ok" else logging.WARNING,
            "tool_call",
            extra=extra,
        )


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    raise TypeError(f"not serializable: {type(value).__name__}")


def to_model_output(result: ToolOutcome) -> str:
    """The string sent to the model: tool data is wrapped as untrusted, never as instructions."""
    if isinstance(result, ToolError):
        payload: dict[str, Any] = {"untrusted_data": None, "status": result.code}
    else:
        payload = {"untrusted_data": result.data, "status": "ok"}
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


__all__ = [
    "Executor",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolTrace",
    "to_model_output",
]
