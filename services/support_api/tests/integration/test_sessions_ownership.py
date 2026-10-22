import hashlib
import io
import json
import logging as stdlib_logging
import re
import uuid
from uuid import uuid4

import pytest
from sqlalchemy import text
from support_api import logging as applog
from support_api.sessions import NotFound, load_owned_conversation

from tests.conftest import ORIGIN, new_client

NOT_FOUND = {"error": "not_found"}


def _token(resp) -> str:
    m = re.match(r"uas_session=([^;]+);", resp.headers["set-cookie"])
    assert m, resp.headers.get("set-cookie")
    return m.group(1)


async def test_other_session_gets_404_on_every_conversation_endpoint(app):  # AC 4
    a, b = new_client(app), new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()[
        "conversation_id"
    ]
    assert (await b.get(f"/api/conversations/{cid}")).status_code == 404
    r = await b.post(
        f"/api/conversations/{cid}/messages",
        headers=ORIGIN,
        json={"client_message_id": str(uuid4()), "text": "oi"},
    )
    assert (
        r.status_code == 404
        and r.json() == (await b.get(f"/api/conversations/{uuid4()}")).json()
    )
    assert r.json() == NOT_FOUND


async def test_foreign_and_nonexistent_are_indistinguishable(app):
    a, b = new_client(app), new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()[
        "conversation_id"
    ]
    foreign = await b.get(f"/api/conversations/{cid}")
    missing = await b.get(f"/api/conversations/{uuid4()}")
    malformed = await b.get("/api/conversations/not-a-uuid")
    for r in (foreign, missing, malformed):
        assert r.status_code == 404 and r.json() == NOT_FOUND
    assert foreign.content == missing.content == malformed.content
    # The owner still sees it.
    assert (await a.get(f"/api/conversations/{cid}")).status_code == 200


async def test_owner_can_create_read_and_post(app):
    a = new_client(app)
    r = await a.post("/api/conversations", json={}, headers=ORIGIN)
    assert r.status_code == 201
    cid = r.json()["conversation_id"]
    assert str(uuid.UUID(cid)) == cid
    got = await a.get(f"/api/conversations/{cid}")
    assert got.status_code == 200 and got.json() == {"turns": []}
    posted = await a.post(
        f"/api/conversations/{cid}/messages",
        headers=ORIGIN,
        json={"client_message_id": str(uuid4()), "text": "oi"},
    )
    assert posted.status_code == 200 and posted.json()["status"] == "completed"
    listed = (await a.get(f"/api/conversations/{cid}")).json()["turns"]
    assert [t["turn_id"] for t in listed] == [posted.json()["turn_id"]]


async def test_cookie_attributes_and_hash_only(app, support_test_engine):
    a = new_client(app)
    r = await a.post("/api/conversations", json={}, headers=ORIGIN)
    cookie = r.headers["set-cookie"]
    token = _token(r)
    attrs = {p.strip().lower() for p in cookie.split(";")[1:]}
    assert {"httponly", "secure", "samesite=lax", "path=/api"} <= attrs
    assert "max-age=28800" in attrs  # 8 h
    assert len(token) >= 43  # token_urlsafe(32)

    cid = r.json()["conversation_id"]
    digest = hashlib.sha256(token.encode()).digest()
    async with support_test_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT s.token_sha256, s.expires_at - s.created_at AS ttl "
                    "FROM support.browser_sessions s "
                    "JOIN support.conversations c ON c.browser_session_id = s.id "
                    "WHERE c.id = :cid"
                ),
                {"cid": cid},
            )
        ).one()
        everything = (
            (
                await conn.execute(
                    text("SELECT token_sha256 FROM support.browser_sessions")
                )
            )
            .scalars()
            .all()
        )
    assert bytes(row.token_sha256) == digest
    assert row.ttl.total_seconds() == 8 * 3600
    assert all(token.encode() not in bytes(v) for v in everything)


async def test_valid_cookie_is_reused_without_new_set_cookie(app):
    a = new_client(app)
    first = await a.post("/api/conversations", json={}, headers=ORIGIN)
    assert "set-cookie" in first.headers
    second = await a.post("/api/conversations", json={}, headers=ORIGIN)
    assert second.status_code == 201 and "set-cookie" not in second.headers
    cid = first.json()["conversation_id"]
    assert (await a.get(f"/api/conversations/{cid}")).status_code == 200


async def _session_count(engine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM support.browser_sessions"))
        ).scalar_one()


async def test_only_create_inserts_a_session(app, support_test_engine):
    c = new_client(app)
    before = await _session_count(support_test_engine)
    for path in (str(uuid4()), "x", "not-a-uuid"):
        r = await c.get(f"/api/conversations/{path}")
        assert r.status_code == 404 and r.json() == NOT_FOUND
        assert "set-cookie" not in r.headers
    r = await c.post(
        f"/api/conversations/{uuid4()}/messages",
        headers=ORIGIN,
        json={"client_message_id": str(uuid4()), "text": "oi"},
    )
    assert r.status_code == 404 and r.json() == NOT_FOUND
    assert "set-cookie" not in r.headers
    assert await _session_count(support_test_engine) == before

    r = await c.post("/api/conversations", json={}, headers=ORIGIN)
    assert r.status_code == 201 and "uas_session=" in r.headers["set-cookie"]
    assert await _session_count(support_test_engine) == before + 1


async def test_unknown_cookie_gets_new_session(app):
    a = new_client(app)
    a.cookies.set(
        "uas_session", "forged-token-value", domain="support.test", path="/api"
    )
    r = await a.post("/api/conversations", json={}, headers=ORIGIN)
    assert r.status_code == 201
    assert _token(r) != "forged-token-value"


async def test_expired_session_loses_conversations_and_is_not_replaced_on_get(
    app, support_test_engine
):
    a = new_client(app)
    r = await a.post("/api/conversations", json={}, headers=ORIGIN)
    cid, token = r.json()["conversation_id"], _token(r)
    async with support_test_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.browser_sessions SET expires_at = now() - interval '1 second' "
                "WHERE token_sha256 = :h"
            ),
            {"h": hashlib.sha256(token.encode()).digest()},
        )
    again = await a.get(f"/api/conversations/{cid}")
    assert again.status_code == 404 and again.json() == NOT_FOUND
    assert "set-cookie" not in again.headers  # GET never issues a session
    fresh = await a.post("/api/conversations", json={}, headers=ORIGIN)
    assert fresh.status_code == 201 and _token(fresh) != token


@pytest.mark.parametrize(
    "origin,ok",
    [
        ("https://demo-5173.app.github.dev", True),
        ("https://demo-5173.app.github.dev/", False),
        ("https://demo-8000.app.github.dev", False),
        ("http://localhost:5173", False),
        (None, False),
    ],
)
async def test_origin_exact_match(app_with_origins, origin, ok):
    c = new_client(app_with_origins)
    headers = {} if origin is None else {"Origin": origin}
    r = await c.post("/api/conversations", json={}, headers=headers)
    if ok:
        assert r.status_code == 201
    else:
        assert r.status_code == 403 and r.json() == {"error": "origin_not_allowed"}


async def test_multiple_allowed_origins(settings):
    from support_api.app import create_app

    app = create_app(
        settings.model_copy(
            update={"allowed_origins": ["http://localhost:5173", "https://x.example"]}
        )
    )
    app.state.test_clients = []
    try:
        c = new_client(app)
        for o in ("http://localhost:5173", "https://x.example"):
            r = await c.post("/api/conversations", json={}, headers={"Origin": o})
            assert r.status_code == 201
    finally:
        for http in app.state.test_clients:
            await http.aclose()
        await app.state.db.engine.dispose()


async def test_non_json_rejected(app):
    c = new_client(app)
    for kwargs in (
        {
            "content": "a=b",
            "headers": {**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        },
        {"content": "{}", "headers": {**ORIGIN, "Content-Type": "text/plain"}},
        {"content": "{}", "headers": ORIGIN},  # no Content-Type at all
    ):
        r = await c.post("/api/conversations", **kwargs)
        assert r.status_code == 415 and r.json() == {"error": "unsupported_media_type"}


async def test_json_content_type_with_charset_is_accepted(app):
    c = new_client(app)
    r = await c.post(
        "/api/conversations",
        content="{}",
        headers={**ORIGIN, "Content-Type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 201


async def test_origin_checked_before_content_type_and_ownership(app):
    a, b = new_client(app), new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()[
        "conversation_id"
    ]
    bad = {"Origin": "https://evil.example"}
    # Cross-site request: 403 whether or not the conversation exists/is owned (no ownership oracle).
    for target in (cid, str(uuid4())):
        r = await b.post(f"/api/conversations/{target}/messages", headers=bad, json={})
        assert r.status_code == 403 and r.json() == {"error": "origin_not_allowed"}
    # Bad origin AND non-JSON: origin wins.
    r = await b.post(
        "/api/conversations", content="x", headers={**bad, "Content-Type": "text/plain"}
    )
    assert r.status_code == 403
    # Valid origin, non-JSON, foreign conversation: media type is checked before ownership.
    r = await b.post(
        f"/api/conversations/{cid}/messages",
        content="x",
        headers={**ORIGIN, "Content-Type": "text/plain"},
    )
    assert r.status_code == 415


async def test_get_does_not_require_origin_or_json(app):
    a = new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()[
        "conversation_id"
    ]
    assert (await a.get(f"/api/conversations/{cid}")).status_code == 200


async def test_load_owned_conversation(app, support_test_engine):
    a, b = new_client(app), new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()[
        "conversation_id"
    ]
    await b.get(f"/api/conversations/{uuid4()}")  # b gets a session
    async with app.state.db.begin() as conn:
        owner = (
            await conn.execute(
                text(
                    "SELECT browser_session_id FROM support.conversations WHERE id = :i"
                ),
                {"i": cid},
            )
        ).scalar_one()
        row = await load_owned_conversation(conn, uuid.UUID(cid), owner)
        assert str(row.id) == cid
        with pytest.raises(NotFound):
            await load_owned_conversation(conn, uuid.UUID(cid), uuid4())
        with pytest.raises(NotFound):
            await load_owned_conversation(conn, uuid4(), owner)


async def test_unknown_cookie_on_get_is_404_without_insert(app, support_test_engine):
    c = new_client(app)
    c.cookies.set(
        "uas_session", "forged-token-value", domain="support.test", path="/api"
    )
    before = await _session_count(support_test_engine)
    r = await c.get(f"/api/conversations/{uuid4()}")
    assert r.status_code == 404 and "set-cookie" not in r.headers
    assert await _session_count(support_test_engine) == before


async def test_unhandled_exception_returns_500_with_correlation_id_and_log(app):
    stream = io.StringIO()
    handler = applog.configure(stream=stream)

    @app.get("/api/_boom")
    async def boom():
        raise RuntimeError("secret 123.456.789-09")

    try:
        c = new_client(app)
        r = await c.get("/api/_boom", headers={"X-Correlation-ID": "boom-12345678"})
    finally:
        stdlib_logging.getLogger().removeHandler(handler)
    assert r.status_code == 500 and r.json() == {"error": "internal_error"}
    assert r.headers["x-correlation-id"] == "boom-12345678"
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed = [x for x in records if x["event"] == "unhandled_exception"]
    assert len(failed) == 1
    assert failed[0]["correlation_id"] == "boom-12345678"
    assert failed[0]["error_code"] == "internal_error"
    assert failed[0].keys() <= applog.ALLOWED_KEYS
    done = [x for x in records if x["event"] == "request_done"][-1]
    assert done["http_status"] == 500 and done["correlation_id"] == "boom-12345678"
    assert "secret" not in stream.getvalue() and "123.456" not in stream.getvalue()


async def test_correlation_id_generated_or_echoed(app):
    c = new_client(app)
    r = await c.get("/api/health")
    generated = r.headers["x-correlation-id"]
    assert str(uuid.UUID(generated)) == generated

    r = await c.get("/api/health", headers={"X-Correlation-ID": "abc-12345678"})
    assert r.headers["x-correlation-id"] == "abc-12345678"

    # Also on error responses (404 / 403).
    r = await c.get(
        f"/api/conversations/{uuid4()}", headers={"X-Correlation-ID": "trace-0001"}
    )
    assert r.status_code == 404 and r.headers["x-correlation-id"] == "trace-0001"
    r = await c.post(
        "/api/conversations", json={}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403 and "x-correlation-id" in r.headers


@pytest.mark.parametrize(
    "bad",
    ["short", "x" * 65, "has_underscore_1", "has space 1234", "emoji-ñ-123456", ""],
)
async def test_malformed_correlation_id_is_replaced(app, bad):
    c = new_client(app)
    try:
        r = await c.get("/api/health", headers={"X-Correlation-ID": bad})
    except (
        UnicodeEncodeError
    ):  # httpx refuses to send non-latin-1; nothing to test then
        return
    got = r.headers["x-correlation-id"]
    assert got != bad and str(uuid.UUID(got)) == got


async def test_correlation_id_boundaries(app):
    c = new_client(app)
    for ok in ("a" * 8, "A1-" + "z" * 61):
        r = await c.get("/api/health", headers={"X-Correlation-ID": ok})
        assert r.headers["x-correlation-id"] == ok
