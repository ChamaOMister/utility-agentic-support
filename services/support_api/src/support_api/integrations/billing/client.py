"""Typed client for the billing service.

All retry logic for billing lives here (global constraint). Every call builds its own tool
``Deadline(8)`` and is further capped by the turn deadline passed in. Each attempt is bounded twice:
httpx timeouts (per phase) and ``asyncio.timeout`` (wall clock for the whole attempt).

Return shape: every method returns ``BillingResult[T]`` (``.value`` and ``.attempts``) so the caller
can put the attempt count in the trace; failures raise ``BillingError`` (also carrying ``attempts``).
"""

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from support_api import logging as uas_logging
from support_api.deadline import Deadline

from .schemas import Account, BillDetail, BillSummary, VerifyResult

logger = logging.getLogger("support_api.billing")

ErrorCode = Literal["upstream_timeout", "upstream_unavailable", "not_found"]
Guard = Callable[[], Awaitable[None]]  # raises to abort before an attempt is sent

ATTEMPT_TIMEOUT_S = 3.0
TOOL_TOTAL_S = 8.0
BACKOFF_BASE_S = (0.2, 0.6)  # GET retries <= 2
JITTER = 0.25
MIN_USEFUL_ATTEMPT_S = (
    0.5  # a retry is only worth sleeping for if this much time remains after it
)


_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _segment(value: str) -> str | None:
    """A value safe to interpolate as one URL path segment, or None. Quoted as defense in depth."""
    if not _SEGMENT.fullmatch(value):
        return None
    return quote(value, safe="")


def _ref_segment(ref: str) -> str:
    seg = _segment(ref)
    if seg is None:
        # Refs come from our own database, never from the model: a bad one is a programming error.
        raise ValueError("invalid customer ref")
    return seg


class BillingError(Exception):
    def __init__(self, code: ErrorCode, attempts: int) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.attempts = attempts


@dataclass(frozen=True)
class BillingResult[T]:
    value: T
    attempts: int


_BILL_LIST = TypeAdapter(list[BillSummary])


class BillingClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        attempt_timeout: float = ATTEMPT_TIMEOUT_S,
        tool_total: float = TOOL_TOTAL_S,
        min_useful_attempt: float = MIN_USEFUL_ATTEMPT_S,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, headers={"X-API-Key": api_key}
        )
        self._sleep = sleep
        self._rng = rng
        self._clock = clock
        self._attempt_timeout = attempt_timeout
        self._tool_total = tool_total
        self._min_useful = min_useful_attempt

    async def aclose(self) -> None:
        await self._http.aclose()

    async def verify(
        self,
        identifier_digits: str,
        birth_date: date,
        phone_last4: str,
        *,
        deadline: Deadline,
        guard: Guard | None = None,
    ) -> BillingResult[VerifyResult]:
        return await self._call(
            "verify",
            "POST",
            "/v1/identity/verify",
            parse=VerifyResult.model_validate,
            retry=False,
            deadline=deadline,
            guard=guard,
            json={
                "identifier": identifier_digits,
                "birth_date": birth_date.isoformat(),
                "phone_last4": phone_last4,
            },
        )

    async def get_account(
        self, ref: str, *, deadline: Deadline, guard: Guard | None = None
    ) -> BillingResult[Account]:
        return await self._call(
            "get_account",
            "GET",
            f"/v1/customers/{_ref_segment(ref)}/account",
            parse=Account.model_validate,
            retry=True,
            deadline=deadline,
            guard=guard,
        )

    async def list_bills(
        self,
        ref: str,
        *,
        status: str | None,
        limit: int,
        deadline: Deadline,
        guard: Guard | None = None,
    ) -> BillingResult[list[BillSummary]]:
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        return await self._call(
            "list_bills",
            "GET",
            f"/v1/customers/{_ref_segment(ref)}/bills",
            parse=_BILL_LIST.validate_python,
            retry=True,
            deadline=deadline,
            guard=guard,
            params=params,
        )

    async def get_bill(
        self, ref: str, bill_id: str, *, deadline: Deadline, guard: Guard | None = None
    ) -> BillingResult[BillDetail]:
        ref_seg = _ref_segment(ref)
        bill_seg = _segment(bill_id)
        if bill_seg is None:
            # bill_id may come from model tool arguments: indistinguishable from a missing bill.
            raise BillingError("not_found", 0)
        return await self._call(
            "get_bill",
            "GET",
            f"/v1/customers/{ref_seg}/bills/{bill_seg}",
            parse=BillDetail.model_validate,
            retry=True,
            deadline=deadline,
            guard=guard,
        )

    async def _call[T](
        self,
        op: str,
        method: str,
        path: str,
        *,
        parse: Callable[[Any], T],
        retry: bool,
        deadline: Deadline,
        guard: Guard | None,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> BillingResult[T]:
        tool = Deadline(self._tool_total, clock=self._clock)
        max_attempts = 1 + (len(BACKOFF_BASE_S) if retry else 0)
        attempts = 0
        last: BillingError | None = None

        for index in range(max_attempts):
            if index > 0:
                pause = BACKOFF_BASE_S[index - 1] * (1 + JITTER * self._rng())
                remaining = min(tool.remaining(), deadline.remaining())
                if pause + self._min_useful > remaining:
                    break  # sleeping would leave no useful attempt: stop with the last error
                await self._sleep(pause)
            if guard is not None:
                await guard()  # anything it raises propagates unchanged

            timeout = min(
                tool.timeout(self._attempt_timeout),
                deadline.timeout(self._attempt_timeout),
            )
            if timeout <= 0:
                last = last or BillingError("upstream_timeout", attempts)
                break
            attempts = index + 1  # every earlier iteration sent exactly one request
            started = time.perf_counter()
            outcome = await self._attempt(method, path, params, json, timeout, parse)
            if isinstance(outcome, _Ok):
                self._log(op, attempts, started, outcome.status, None)
                return BillingResult(outcome.value, attempts)
            last = BillingError(outcome.code, attempts)
            self._log(op, attempts, started, outcome.status, outcome.code)
            if not outcome.retryable:
                break

        assert last is not None
        raise last

    async def _attempt[T](
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: Any,
        timeout: float,
        parse: Callable[[Any], T],
    ) -> "_Ok[T] | _Failed":
        headers = {}
        cid = uas_logging.correlation_id.get()
        if cid is not None:
            headers["X-Correlation-ID"] = cid
        try:
            async with asyncio.timeout(
                timeout
            ):  # total wall-clock bound for this attempt
                response = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=httpx.Timeout(
                        timeout
                    ),  # per phase; the line above bounds the total
                )
        except (httpx.TimeoutException, TimeoutError):
            return _Failed("upstream_timeout", True, None)
        except httpx.TransportError:
            return _Failed("upstream_unavailable", True, None)
        except httpx.HTTPError:
            return _Failed("upstream_unavailable", False, None)

        status = response.status_code
        if status == 404:
            return _Failed("not_found", False, status)
        if status >= 500:
            return _Failed("upstream_unavailable", True, status)
        if (
            status != 200
        ):  # 401 and other 4xx: our configuration or contract, retrying cannot help
            return _Failed("upstream_unavailable", False, status)
        try:
            return _Ok(parse(response.json()), status)
        except (ValueError, ValidationError):
            return _Failed("upstream_unavailable", False, status)  # contract violation

    def _log(
        self,
        op: str,
        attempt: int,
        started: float,
        status: int | None,
        error_code: str | None,
    ) -> None:
        extra: dict[str, Any] = {
            "tool_name": op,
            "attempt": attempt,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if status is not None:
            extra["http_status"] = status
        if error_code is not None:
            extra["error_code"] = error_code
        logger.log(
            logging.INFO if error_code is None else logging.WARNING,
            "billing_attempt",
            extra=extra,
        )


@dataclass(frozen=True)
class _Ok[T]:
    value: T
    status: int


@dataclass(frozen=True)
class _Failed:
    code: ErrorCode
    retryable: bool
    status: int | None
