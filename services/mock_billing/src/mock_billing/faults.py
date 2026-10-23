"""Deterministic fault injection for the mock billing API (dev/test only).

Rules live in process memory on the app instance (`app.state.faults`). A rule matches the
route *name* (not the raw path, so path parameters never break matching) and, optionally, the
`ref` path parameter. `verify` has no customer_ref in its path, so a rule that sets
`customer_ref` never matches it.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import ApiError, _key_matches

RouteName = Literal["verify", "account", "list_bills", "bill_detail"]
TIMEOUT_SLEEP_SECONDS = 30  # longer than any client timeout; the caller gives up first
MAX_DELAY_MS = 60_000
_EFFECT_RE = re.compile(r"^(timeout|http_500|unavailable|delay_ms:\d{1,9})$")

Sleep = Callable[[float], Awaitable[None]]


class FaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_name: RouteName
    customer_ref: str | None = Field(default=None, min_length=1, max_length=64)
    effect: str

    @field_validator("effect")
    @classmethod
    def _valid_effect(cls, value: str) -> str:
        if not _EFFECT_RE.fullmatch(value):
            raise ValueError(
                "effect must be timeout, http_500, unavailable or delay_ms:N"
            )
        if (
            value.startswith("delay_ms:")
            and int(value.partition(":")[2]) > MAX_DELAY_MS
        ):
            raise ValueError(f"delay_ms must be <= {MAX_DELAY_MS}")
        return value


class FaultRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[FaultRule] = Field(max_length=100)


async def require_fault_admin_key(
    request: Request, x_fault_admin_key: Annotated[str | None, Header()] = None
) -> None:
    # A separate secret from the billing API key; an empty configured key never authenticates.
    if not _key_matches(x_fault_admin_key, request.app.state.settings.fault_admin_key):
        raise ApiError(401, "unauthorized")


admin_router = APIRouter(
    prefix="/_dev", dependencies=[Depends(require_fault_admin_key)]
)


@admin_router.put("/faults", status_code=204)
async def put_faults(body: FaultRules, request: Request) -> Response:
    request.app.state.faults = list(body.rules)
    return Response(status_code=204)


@admin_router.delete("/faults", status_code=204)
async def delete_faults(request: Request) -> Response:
    request.app.state.faults = []
    return Response(status_code=204)


async def apply_faults(request: Request) -> None:
    """Route dependency: apply the first matching rule. No-op when no rules are set."""
    rules: list[FaultRule] = getattr(request.app.state, "faults", [])
    if not rules:
        return
    route_name = getattr(request.scope.get("route"), "name", None)
    ref = request.path_params.get("ref")
    for rule in rules:
        if rule.route_name != route_name:
            continue
        if rule.customer_ref is not None and rule.customer_ref != ref:
            continue
        await _apply(rule.effect, request.app.state.sleep)
        return


async def _apply(effect: str, sleep: Sleep) -> None:
    if effect == "http_500":
        raise ApiError(500, "injected_fault")
    if effect == "unavailable":
        raise ApiError(503, "service_unavailable")
    if effect == "timeout":
        await sleep(TIMEOUT_SLEEP_SECONDS)
    elif effect.startswith("delay_ms:"):
        await sleep(int(effect.partition(":")[2]) / 1000)
