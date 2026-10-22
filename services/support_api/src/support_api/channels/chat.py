"""Chat channel routes. Task 6: conversation create/read and the ownership-guarded messages stub."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..sessions import (
    CurrentSession,
    OwnedConversation,
    require_json_and_origin,
)

router = APIRouter(prefix="/api/conversations")


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
    conversation: OwnedConversation,
) -> dict[str, list]:
    return {"turns": []}  # filled in Task 17


@router.post(
    "/{conversation_id}/messages", dependencies=[Depends(require_json_and_origin)]
)
async def post_message(conversation: OwnedConversation) -> JSONResponse:
    # Ownership is enforced; the turn pipeline arrives in Task 17.
    return JSONResponse({"error": "not_implemented"}, status_code=501)
