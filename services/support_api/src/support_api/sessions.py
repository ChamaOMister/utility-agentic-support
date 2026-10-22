"""Browser sessions, conversation ownership and CSRF checks (spec §5.1).

Order of checks on a state-changing endpoint (dependency declaration order):
  1. Origin must exactly match ``ALLOWED_ORIGINS``            -> 403 ``origin_not_allowed``
  2. ``Content-Type`` must be ``application/json``            -> 415 ``unsupported_media_type``
  3. Browser session from the cookie (created if absent)
  4. Conversation loaded by ``(conversation_id, session)``    -> 404 ``not_found``
Origin and media type come first so a cross-site request learns nothing about which conversation ids
exist (no ownership oracle). A same-origin caller that sends a valid Origin still sees the 404.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.datastructures import MutableHeaders

COOKIE_NAME = "uas_session"
SESSION_TTL_S = 8 * 3600  # absolute lifetime
_PENDING_COOKIE = "uas_pending_session_cookie"


class ApiError(Exception):
    status_code = 500
    code = "internal_error"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class OriginNotAllowed(ApiError):
    status_code = 403
    code = "origin_not_allowed"


class UnsupportedMediaType(ApiError):
    status_code = 415
    code = "unsupported_media_type"


@dataclass(frozen=True)
class BrowserSession:
    id: UUID
    expires_at: datetime


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _set_cookie_value(token: str) -> str:
    return f"{COOKIE_NAME}={token}; Max-Age={SESSION_TTL_S}; Path=/api; HttpOnly; Secure; SameSite=Lax"


async def current_session(request: Request) -> BrowserSession:
    """The caller's browser session; a missing, unknown or expired cookie yields a new session.

    The new cookie is queued on the request and attached by ``SessionCookieMiddleware`` so that it is
    also sent on error responses (e.g. the 404 for a cookie-less caller).
    """
    db = request.app.state.db
    presented = request.cookies.get(COOKIE_NAME)
    async with db.begin() as conn:
        if presented:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, expires_at FROM support.browser_sessions "
                        "WHERE token_sha256 = :h AND expires_at > now()"
                    ),
                    {"h": hash_token(presented)},
                )
            ).first()
            if row is not None:
                return BrowserSession(row.id, row.expires_at)
        token = secrets.token_urlsafe(32)  # 256 bits
        row = (
            await conn.execute(
                text(
                    "INSERT INTO support.browser_sessions (token_sha256, expires_at) "
                    "VALUES (:h, now() + make_interval(secs => :ttl)) RETURNING id, expires_at"
                ),
                {"h": hash_token(token), "ttl": SESSION_TTL_S},
            )
        ).one()
    setattr(request.state, _PENDING_COOKIE, _set_cookie_value(token))
    return BrowserSession(row.id, row.expires_at)


async def require_json_and_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in request.app.state.settings.allowed_origins:
        raise OriginNotAllowed
    media_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if media_type != "application/json":
        raise UnsupportedMediaType


async def load_owned_conversation(
    conn: AsyncConnection, conversation_id: UUID, session_id: UUID
) -> Row:
    """Load by (id, browser session). Foreign and nonexistent are indistinguishable: NotFound."""
    row = (
        await conn.execute(
            text(
                "SELECT * FROM support.conversations "
                "WHERE id = :c AND browser_session_id = :s"
            ),
            {"c": conversation_id, "s": session_id},
        )
    ).first()
    if row is None:
        raise NotFound
    return row


async def owned_conversation(
    conversation_id: str,
    request: Request,
    session: Annotated[BrowserSession, Depends(current_session)],
) -> Row:
    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise NotFound from None  # malformed id looks exactly like a missing one
    async with request.app.state.db.begin() as conn:
        return await load_owned_conversation(conn, cid, session.id)


class SessionCookieMiddleware:
    """Attach the Set-Cookie queued by ``current_session`` to whatever response goes out."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message) -> None:
            if message["type"] == "http.response.start":
                cookie = scope.get("state", {}).get(_PENDING_COOKIE)
                if cookie:
                    MutableHeaders(scope=message).append("set-cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def install(app: FastAPI) -> None:
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_middleware(SessionCookieMiddleware)


CurrentSession = Annotated[BrowserSession, Depends(current_session)]
OwnedConversation = Annotated[Row, Depends(owned_conversation)]
