"""Chat channel routes: conversation create/read and message submission (spec §5.1, §5.5)."""

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import logging as uas_logging
from ..sessions import (
    CurrentSession,
    OwnedConversation,
    require_json_and_origin,
)
from ..turns.recovery import recover_conversation
from ..turns.service import TurnLimits, submit_message

router = APIRouter(prefix="/api/conversations")

MAX_TEXT_CODE_POINTS = 2000

_NEEDS_RECOVERY = text(
    "SELECT active_turn_id IS NOT NULL AND active_turn_lease_expires_at <= now() "
    "FROM support.conversations WHERE id = :c"
)
_LOCK = text("SELECT 1 FROM support.conversations WHERE id = :c FOR UPDATE")
_TURNS = text(
    "SELECT id, client_message_id, status, reply, trace, created_at, finished_at "
    "FROM support.turns WHERE conversation_id = :c ORDER BY created_at, id"
)


def _invalid(code: str = "invalid_request") -> JSONResponse:
    return JSONResponse({"error": code}, status_code=422)


@router.post("", status_code=201, dependencies=[Depends(require_json_and_origin)])
async def create_conversation(
    request: Request, session: CurrentSession
) -> dict[str, str]:
    async with request.app.state.db.begin() as conn:
        conversation_id = (
            await conn.execute(
                text(
                    "INSERT INTO support.conversations (browser_session_id) "
                    "VALUES (:s) RETURNING id"
                ),
                {"s": session.id},
            )
        ).scalar_one()
    return {"conversation_id": str(conversation_id)}


@router.get("/{conversation_id}")
async def get_conversation(
    request: Request, conversation: OwnedConversation
) -> dict[str, list]:
    """The turns in creation order (metadata and replies only, never tool payloads)."""
    db = request.app.state.db
    cid = conversation.id
    async with db.begin() as conn:
        if (await conn.execute(_NEEDS_RECOVERY, {"c": cid})).scalar_one():
            await conn.execute(_LOCK, {"c": cid})
            await recover_conversation(conn, cid)  # re-checks the lease under the lock
        rows = (await conn.execute(_TURNS, {"c": cid})).all()
    return {
        "turns": [
            {
                "turn_id": str(r.id),
                "client_message_id": str(r.client_message_id),
                "status": r.status,
                "reply": r.reply,
                "trace": r.trace,
                "created_at": r.created_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in rows
        ]
    }


@router.post(
    "/{conversation_id}/messages", dependencies=[Depends(require_json_and_origin)]
)
async def post_message(
    request: Request, conversation: OwnedConversation
) -> JSONResponse:
    # The body is parsed here, after the origin, media-type and ownership checks have passed.
    try:
        body: Any = json.loads(await request.body())
    except ValueError:
        return _invalid()
    if not isinstance(body, dict):
        return _invalid()
    raw_id, message_text = body.get("client_message_id"), body.get("text")
    if not isinstance(raw_id, str) or not isinstance(message_text, str):
        return _invalid()
    try:
        client_message_id = UUID(raw_id)
    except ValueError:
        return _invalid()
    # Code points, not bytes or UTF-16 units: 2,000 "ç" or emoji are allowed (Review Focus 4).
    if not 1 <= len(message_text) <= MAX_TEXT_CODE_POINTS:
        return _invalid("text_length")

    state = request.app.state
    result = await submit_message(
        state.db,
        state.core,
        session_id=conversation.browser_session_id,
        conversation_id=conversation.id,
        client_message_id=client_message_id,
        text=message_text,
        correlation_id=uas_logging.correlation_id.get() or str(uuid4()),
        limits=TurnLimits.from_settings(state.settings),
        clock=state.clock,
    )
    return JSONResponse(result.body, status_code=result.http_status)
