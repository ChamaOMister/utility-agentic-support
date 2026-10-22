"""Budget reservations and the ``llm_calls`` ledger (spec §5.6, §5.5 recovery step 3).

Money is ``Decimal`` / ``NUMERIC(12,6)``; every amount is rounded *up* (``ROUND_CEILING``) to six
places so an application estimate never undershoots.

Row-lock order in every function: ``conversations`` (held by ``fenced`` or by the recovery caller)
-> ``llm_calls`` (a status-conditional UPDATE on the call's own row) -> ``budget_accounts``
('project') -> ``conversation_budgets``.

:func:`reserve` touches the budget rows first and inserts its ``llm_calls`` row last. That does not
break the order: inserting a brand-new uuid row locks nothing that already exists, so no other
transaction can hold or wait on it.

Every ledger transition is a status-conditional UPDATE that reads the *prior* status, so a repeat
(or a racing recovery) matches zero rows, returns ``False`` and leaves both budget rows untouched.
``live_blocked_reason`` only ever goes from NULL to a value.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from support_api.db import Database
from support_api.settings import Settings
from support_api.turns.claim import Claim
from support_api.usage.pricing import Rate

DEFAULT_CONVERSATION_CAP_USD: Decimal = Settings.model_fields[
    "conversation_budget_usd"
].default
INTERRUPTED_REASON = "interrupted_after_possible_send"

_PLACES = Decimal("0.000001")
_PER_TOKEN = Decimal("0.000001")  # rates are per million tokens


@dataclass(frozen=True)
class Usage:
    input_tokens: int  # total input tokens, cached ones included
    cached_input_tokens: int
    output_tokens: int


class BudgetExceeded(Exception):
    def __init__(self, scope: Literal["project", "conversation"]) -> None:
        super().__init__(f"{scope} budget exceeded")
        self.scope = scope


def _ceil6(value: Decimal) -> Decimal:
    return value.quantize(_PLACES, rounding=ROUND_CEILING)


def worst_case_usd(input_tokens: int, max_output_tokens: int, rate: Rate) -> Decimal:
    """Uncached input plus the full output allowance, rounded up to 6 places."""
    if input_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token counts must not be negative")
    with localcontext() as ctx:
        ctx.prec = 50
        total = (
            input_tokens * rate.input_per_mtok
            + max_output_tokens * rate.output_per_mtok
        ) * _PER_TOKEN
        return _ceil6(total)


def actual_usd(usage: Usage, rate: Rate) -> Decimal:
    """Priced usage: non-cached input, cached input (cached rate if set), output; rounded up."""
    if (
        usage.input_tokens < 0
        or usage.cached_input_tokens < 0
        or usage.output_tokens < 0
        or usage.cached_input_tokens > usage.input_tokens
    ):
        raise ValueError("inconsistent token usage")
    cached_rate = (
        rate.cached_input_per_mtok
        if rate.cached_input_per_mtok is not None
        else rate.input_per_mtok
    )
    with localcontext() as ctx:
        ctx.prec = 50
        total = (
            (usage.input_tokens - usage.cached_input_tokens) * rate.input_per_mtok
            + usage.cached_input_tokens * cached_rate
            + usage.output_tokens * rate.output_per_mtok
        ) * _PER_TOKEN
        return _ceil6(total)


# --- SQL ---------------------------------------------------------------------------------------

_PROJECT_RESERVE = text(
    "UPDATE support.budget_accounts "
    "SET reserved_usd = reserved_usd + :amt "
    "WHERE id = 'project' AND spent_usd + reserved_usd + uncertain_usd + :amt <= cap_usd "
    "RETURNING id"
)
_CONVERSATION_ROW = text(
    "INSERT INTO support.conversation_budgets (conversation_id, cap_usd) "
    "VALUES (:c, :cap) ON CONFLICT (conversation_id) DO NOTHING"
)
_CONVERSATION_RESERVE = text(
    "UPDATE support.conversation_budgets "
    "SET reserved_usd = reserved_usd + :amt "
    "WHERE conversation_id = :c AND spent_usd + reserved_usd + uncertain_usd + :amt <= cap_usd "
    "RETURNING conversation_id"
)
_INSERT_CALL = text(
    "INSERT INTO support.llm_calls "
    "(conversation_id, turn_id, kind, model, service_tier, attempt, status, reserved_usd) "
    "VALUES (:c, :t, :kind, :model, :tier, :attempt, 'reserved', :amt) RETURNING id"
)
_OWNER = text("SELECT conversation_id FROM support.llm_calls WHERE id = :id")

# The CTE locks the row and exposes its prior status; the UPDATE is conditional on that status.
_TRANSITION = text(
    "WITH old AS ("
    "  SELECT id, status, reserved_usd FROM support.llm_calls "
    "  WHERE id = :id AND status = ANY(CAST(:from_statuses AS text[])) FOR UPDATE"
    ") "
    "UPDATE support.llm_calls l SET status = :to_status, "
    "  outcome_class = COALESCE(CAST(:outcome_class AS text), l.outcome_class), "
    "  estimated_usd = COALESCE(CAST(:estimated AS numeric), l.estimated_usd), "
    "  input_tokens = COALESCE(CAST(:in_tok AS integer), l.input_tokens), "
    "  output_tokens = COALESCE(CAST(:out_tok AS integer), l.output_tokens), "
    "  latency_ms = COALESCE(CAST(:latency_ms AS integer), l.latency_ms), "
    "  provider_request_id = COALESCE(CAST(:request_id AS text), l.provider_request_id), "
    "  service_tier = COALESCE(CAST(:tier AS text), l.service_tier) "
    "FROM old WHERE l.id = old.id "
    "RETURNING old.status AS prior, l.reserved_usd AS amount, l.conversation_id AS conversation_id"
)
_RECOVER_ROWS = text(
    "WITH old AS ("
    "  SELECT id, reserved_usd FROM support.llm_calls "
    "  WHERE conversation_id = :c AND turn_id = :t AND status = 'reserved' "
    "  ORDER BY id FOR UPDATE"
    "), moved AS ("
    "  UPDATE support.llm_calls l SET status = 'uncertain', "
    "    outcome_class = COALESCE(l.outcome_class, 'interrupted') "
    "  FROM old WHERE l.id = old.id RETURNING old.reserved_usd AS amount"
    ") SELECT count(*) AS n, COALESCE(sum(amount), 0) AS total FROM moved"
)
_PROJECT_MOVE = text(
    "UPDATE support.budget_accounts SET reserved_usd = reserved_usd + :dr, "
    "uncertain_usd = uncertain_usd + :du, spent_usd = spent_usd + :ds WHERE id = 'project'"
)
_CONVERSATION_MOVE = text(
    "UPDATE support.conversation_budgets SET reserved_usd = reserved_usd + :dr, "
    "uncertain_usd = uncertain_usd + :du, spent_usd = spent_usd + :ds "
    "WHERE conversation_id = :c"
)
_BLOCK = text(
    "UPDATE support.conversations SET live_blocked_reason = :r "
    "WHERE id = :c AND live_blocked_reason IS NULL"
)


async def _move(
    conn: AsyncConnection,
    conversation_id: UUID,
    *,
    reserved: Decimal = Decimal(0),
    uncertain: Decimal = Decimal(0),
    spent: Decimal = Decimal(0),
) -> None:
    """Apply signed deltas to the project row, then the conversation row (lock order)."""
    deltas = {"dr": reserved, "du": uncertain, "ds": spent}
    await conn.execute(_PROJECT_MOVE, deltas)
    await conn.execute(_CONVERSATION_MOVE, {**deltas, "c": conversation_id})


# --- operations --------------------------------------------------------------------------------


async def reserve(
    conn: AsyncConnection,
    claim: Claim,
    *,
    amount: Decimal,
    kind: str,
    model: str,
    tier: str,
    attempt: int,
    conversation_cap_usd: Decimal | None = None,
) -> UUID:
    """Reserve ``amount`` against the project and the conversation cap; returns the llm_call id.

    Call inside ``fenced``. Raises :class:`BudgetExceeded` (project checked first) when a cap would
    be passed; the fenced transaction then rolls back, leaving neither account changed.
    """
    if not amount.is_finite() or amount <= 0:
        raise ValueError("reservation amount must be a positive finite Decimal")
    amount = _ceil6(amount)
    cap = (
        DEFAULT_CONVERSATION_CAP_USD
        if conversation_cap_usd is None
        else conversation_cap_usd
    )
    if (await conn.execute(_PROJECT_RESERVE, {"amt": amount})).first() is None:
        raise BudgetExceeded("project")
    await conn.execute(_CONVERSATION_ROW, {"c": claim.conversation_id, "cap": cap})
    if (
        await conn.execute(
            _CONVERSATION_RESERVE, {"amt": amount, "c": claim.conversation_id}
        )
    ).first() is None:
        raise BudgetExceeded("conversation")
    return (
        await conn.execute(
            _INSERT_CALL,
            {
                "c": claim.conversation_id,
                "t": claim.turn_id,
                "kind": kind,
                "model": model,
                "tier": tier,
                "attempt": attempt,
                "amt": amount,
            },
        )
    ).scalar_one()


async def _assert_owned(conn: AsyncConnection, claim: Claim, llm_call_id: UUID) -> bool:
    """False if the call does not exist; ValueError if it belongs to another conversation."""
    owner = (await conn.execute(_OWNER, {"id": llm_call_id})).scalar_one_or_none()
    if owner is None:
        return False
    if owner != claim.conversation_id:
        raise ValueError("llm_call does not belong to the claim's conversation")
    return True


async def release(
    conn: AsyncConnection,
    claim: Claim,
    llm_call_id: UUID,
    *,
    outcome_class: str | None = None,
    latency_ms: int | None = None,
    provider_request_id: str | None = None,
) -> bool:
    """``reserved -> released``; never applies to ``uncertain``. Call inside ``fenced``."""
    if not await _assert_owned(conn, claim, llm_call_id):
        return False
    row = (
        await conn.execute(
            _TRANSITION,
            {
                "id": llm_call_id,
                "from_statuses": ["reserved"],
                "to_status": "released",
                "outcome_class": outcome_class,
                "estimated": None,
                "in_tok": None,
                "out_tok": None,
                "latency_ms": latency_ms,
                "request_id": provider_request_id,
                "tier": None,
            },
        )
    ).first()
    if row is None:
        return False
    await _move(conn, claim.conversation_id, reserved=-row.amount)
    return True


async def mark_uncertain(
    conn: AsyncConnection,
    claim: Claim,
    llm_call_id: UUID,
    outcome_class: str,
    block_reason: str | None,
    *,
    latency_ms: int | None = None,
    provider_request_id: str | None = None,
    service_tier: str | None = None,
) -> bool:
    """``reserved -> uncertain``: the full amount stays held. Call inside ``fenced``.

    ``block_reason``, when given, is written to ``conversations.live_blocked_reason`` only if that
    is still NULL, and only when this call performed the transition.
    """
    if not await _assert_owned(conn, claim, llm_call_id):
        return False
    row = (
        await conn.execute(
            _TRANSITION,
            {
                "id": llm_call_id,
                "from_statuses": ["reserved"],
                "to_status": "uncertain",
                "outcome_class": outcome_class,
                "estimated": None,
                "in_tok": None,
                "out_tok": None,
                "latency_ms": latency_ms,
                "request_id": provider_request_id,
                "tier": service_tier,
            },
        )
    ).first()
    if row is None:
        return False
    await _move(conn, claim.conversation_id, reserved=-row.amount, uncertain=row.amount)
    if block_reason is not None:
        await conn.execute(_BLOCK, {"r": block_reason, "c": claim.conversation_id})
    return True


async def settle(
    db: Database,
    llm_call_id: UUID,
    usage: Usage,
    rate: Rate,
    *,
    outcome_class: str | None = None,
    latency_ms: int | None = None,
    provider_request_id: str | None = None,
    service_tier: str | None = None,
) -> bool:
    """``reserved|uncertain -> settled`` with usage actually received. Unfenced (§5.5).

    Removes the held amount from whichever bucket held it and adds the priced usage to ``spent``.
    Keyed by the call's own row, so a stale worker or a repeat is harmless (``False``).
    """
    actual = actual_usd(usage, rate)
    async with db.begin() as conn:
        row = (
            await conn.execute(
                _TRANSITION,
                {
                    "id": llm_call_id,
                    "from_statuses": ["reserved", "uncertain"],
                    "to_status": "settled",
                    "outcome_class": outcome_class,
                    "estimated": actual,
                    "in_tok": usage.input_tokens,
                    "out_tok": usage.output_tokens,
                    "latency_ms": latency_ms,
                    "request_id": provider_request_id,
                    "tier": service_tier,
                },
            )
        ).first()
        if row is None:
            return False
        held = row.amount
        await _move(
            conn,
            row.conversation_id,
            reserved=-held if row.prior == "reserved" else Decimal(0),
            uncertain=-held if row.prior == "uncertain" else Decimal(0),
            spent=actual,
        )
        return True


async def recover_reservations(
    conn: AsyncConnection, conversation_id: UUID, turn_id: UUID
) -> int:
    """Move every ``reserved`` call of ``turn_id`` to ``uncertain`` (§5.5 step 3).

    Runs in the caller's transaction, which already holds the conversation row lock. Sets
    ``live_blocked_reason`` (only if NULL) when at least one row moved. Returns the rows moved.
    """
    moved = (
        await conn.execute(_RECOVER_ROWS, {"c": conversation_id, "t": turn_id})
    ).one()
    if moved.n == 0:
        return 0
    await _move(conn, conversation_id, reserved=-moved.total, uncertain=moved.total)
    await conn.execute(_BLOCK, {"r": INTERRUPTED_REASON, "c": conversation_id})
    return moved.n
