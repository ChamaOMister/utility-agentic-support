"""Assurance state of a conversation, read with the database clock (spec §5.2)."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_ASSURANCE = text(
    "SELECT assurance_level, customer_ref, "
    "(assurance_level <> 'none' AND verified_expires_at <= now()) AS expired "
    "FROM support.conversation_auth WHERE conversation_id = :c"
)

_LOCKED = text(
    "SELECT COALESCE(locked_until > now(), false) FROM support.conversation_auth "
    "WHERE conversation_id = :c"
)


@dataclass(frozen=True)
class Assurance:
    level: Literal["none", "kba", "step_up"]
    customer_ref: str | None
    expired: bool


async def current_assurance(conn: AsyncConnection, conversation_id: UUID) -> Assurance:
    """The conversation's current assurance.

    After ``verified_expires_at`` the level is ``none`` with no ``customer_ref``, and ``expired`` is
    true so callers can tell "verification expired" from "never verified". Expiry is judged by the
    database clock.
    """
    row = (await conn.execute(_ASSURANCE, {"c": conversation_id})).first()
    if row is None or row.assurance_level == "none":
        return Assurance("none", None, False)
    if row.expired:
        return Assurance("none", None, True)
    return Assurance(row.assurance_level, row.customer_ref, False)


async def conversation_locked(conn: AsyncConnection, conversation_id: UUID) -> bool:
    """True while the conversation's own failure lock (``locked_until``) is in the future."""
    return bool(
        (await conn.execute(_LOCKED, {"c": conversation_id})).scalar_one_or_none()
    )
