"""Shared test fixtures for support_api (extended by later tasks)."""

import asyncio
import functools
import json
import os
import socket
import uuid
from pathlib import Path

import httpcore
import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
from support_api.checkpoints import Checkpoints, SimulatedCrash

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_DIR = REPO_ROOT / "services" / "support_api"
PERSONAS_PATH = REPO_ROOT / "data" / "synthetic" / "personas.json"
TEST_ORIGIN = "https://demo-5173.app.github.dev"
ORIGIN = {"Origin": TEST_ORIGIN}


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser; real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_ROOT / ".env")


@pytest.fixture
async def support_test_engine():
    engine = create_async_engine(os.environ["SUPPORT_TEST_DATABASE_URL"])
    try:
        yield engine
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Network guard: no test may reach api.openai.com for real (no paid calls, ever).
# ---------------------------------------------------------------------------
class RealNetworkBlocked(AssertionError):
    """A test tried to open a real connection to a blocked host."""


BLOCKED_HOSTS = ("api.openai.com",)


def _is_blocked(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    return isinstance(host, str) and host.rstrip(".").lower() in BLOCKED_HOSTS


def _block(host: object) -> None:
    if _is_blocked(host):
        raise RealNetworkBlocked(
            f"real network access to {host} is blocked in tests; mock it with respx"
        )


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """Block real connections to api.openai.com at the lowest HTTP layer and at the socket layer.

    The HTTP guard patches httpcore's connection pools (the real transport underneath
    httpx.AsyncHTTPTransport / HTTPTransport). respx's default "httpcore" mocker patches those same
    methods when a mock router is entered, after this fixture, so it wraps the guard: mocked routes
    are answered first and only pass-through traffic reaches the guard. Patching httpx's transport
    methods themselves would sit *above* respx's mock and block mocked routes too. The wrappers
    keep the original method names (functools.wraps) because respx only mocks methods it recognises
    by name.
    """
    real_async = httpcore.AsyncConnectionPool.handle_async_request
    real_sync = httpcore.ConnectionPool.handle_request
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    @functools.wraps(real_async)
    async def guarded_async(self, request):
        _block(request.url.host)
        return await real_async(self, request)

    @functools.wraps(real_sync)
    def guarded_sync(self, request):
        _block(request.url.host)
        return real_sync(self, request)

    def guarded_create_connection(address, *args, **kwargs):
        _block(address[0])
        return real_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        _block(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(
        httpcore.AsyncConnectionPool, "handle_async_request", guarded_async
    )
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", guarded_sync)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


# ---------------------------------------------------------------------------
# Clock, personas, migrated database, app clients.
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def personas() -> list[dict]:
    return json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))


def _alembic_config(url: str) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def support_db():
    """Rebuild the support schema from migrations (also exercises downgrade) in uas_test."""
    from support_api.settings import Settings

    settings = Settings(
        support_database_url=os.environ["SUPPORT_TEST_DATABASE_URL"],
        billing_base_url="http://billing.test",
        billing_api_key="test-service-key",
        identifier_hash_key="test-hash-key",
        personas_path=PERSONAS_PATH,
        allowed_origins=[TEST_ORIGIN],
        llm_provider="scripted",
        live_llm_enabled=False,
        openai_api_key=None,
        demo_data_endpoint_enabled=True,
    )
    cfg = _alembic_config(settings.support_database_url)
    os.environ["PROJECT_BUDGET_USD"] = "20"
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return settings


@pytest.fixture
def settings(support_db):
    return support_db


async def _client_for(settings):
    from support_api.app import create_app

    app = create_app(settings)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://support.test"
    )
    return app, http


@pytest_asyncio.fixture
async def client(settings):
    app, http = await _client_for(settings)
    async with http:
        yield http
    await app.state.db.engine.dispose()


@pytest_asyncio.fixture
async def client_demo_off(settings):
    app, http = await _client_for(
        settings.model_copy(update={"demo_data_endpoint_enabled": False})
    )
    async with http:
        yield http
    await app.state.db.engine.dispose()


# https base URL: the session cookie is Secure, so the client's cookie jar only stores and sends it
# over https.
def new_client(app) -> httpx.AsyncClient:
    """A fresh client (own cookie jar = own browser session) bound to ``app``; closed by the fixture."""
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://support.test"
    )
    app.state.test_clients.append(http)
    return http


async def _make_app(settings):
    from support_api.app import create_app

    app = create_app(settings)
    app.state.test_clients = []
    return app


async def _close_app(app) -> None:
    for http in app.state.test_clients:
        await http.aclose()
    await app.state.db.engine.dispose()


@pytest_asyncio.fixture
async def app(settings):
    app = await _make_app(settings)
    yield app
    await _close_app(app)


@pytest_asyncio.fixture
async def app_with_origins(settings):
    """ALLOWED_ORIGINS="https://demo-5173.app.github.dev" (the documented Codespaces shape)."""
    app = await _make_app(
        settings.model_copy(update={"allowed_origins": [TEST_ORIGIN]})
    )
    yield app
    await _close_app(app)


# ---------------------------------------------------------------------------
# Live mock-billing subprocess (session-scoped; reused by later integration tests).
# ---------------------------------------------------------------------------
BILLING_TEST_API_KEY = "test-service-key"
BILLING_TEST_FAULT_ADMIN_KEY = "test-fault-admin-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def billing_server(tmp_path_factory):
    """Run mock-billing (uvicorn, fault injection on) on a free localhost port against ``uas_test``.

    The billing schema is rebuilt from mock-billing's own migrations and seeded with the personas,
    exactly as mock-billing's tests do. Yields a namespace with ``base_url``, ``api_key`` and
    ``fault_admin_key``.
    """
    import asyncio
    import subprocess
    import sys
    import time
    from types import SimpleNamespace

    from mock_billing.seed import seed_from_file

    url = os.environ["BILLING_TEST_DATABASE_URL"]
    cfg = Config(str(REPO_ROOT / "services" / "mock_billing" / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "services" / "mock_billing" / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    asyncio.run(seed_from_file(url, PERSONAS_PATH))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "BILLING_DATABASE_URL": url,
        "BILLING_API_KEY": BILLING_TEST_API_KEY,
        "FAULT_INJECTION_ENABLED": "true",
        "FAULT_ADMIN_KEY": BILLING_TEST_FAULT_ADMIN_KEY,
        "PERSONAS_PATH": str(PERSONAS_PATH),
    }
    log_path = tmp_path_factory.mktemp("billing_server") / "uvicorn.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mock_billing.main:build",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"mock-billing exited early:\n{log_path.read_text(errors='replace')}"
                    )
                try:
                    if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"mock-billing not healthy:\n{log_path.read_text(errors='replace')}"
                    )
                time.sleep(0.1)
            yield SimpleNamespace(
                base_url=base_url,
                api_key=BILLING_TEST_API_KEY,
                fault_admin_key=BILLING_TEST_FAULT_ADMIN_KEY,
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# Turn-claim helpers (Task 8): database, conversation, checkpoints, lease control.
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db(settings):
    from sqlalchemy import text
    from support_api.db import Database

    database = Database(settings.support_database_url)
    # Identifier lockouts are keyed by hash, not conversation, so state would leak between tests
    # that share a hash: start every test with no attempt slots and no identifier counters.
    async with database.begin() as conn:
        await conn.execute(text("DELETE FROM support.verification_attempts"))
        await conn.execute(text("DELETE FROM support.identifier_lockouts"))
    try:
        yield database
    finally:
        await database.dispose()


async def make_conversation(db):
    """A browser session plus one conversation row; returns the conversation id."""
    from sqlalchemy import text

    async with db.begin() as conn:
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO support.browser_sessions (token_sha256, expires_at) "
                    "VALUES (:h, now() + interval '1 hour') RETURNING id"
                ),
                {"h": uuid.uuid4().bytes},
            )
        ).scalar_one()
        return (
            await conn.execute(
                text(
                    "INSERT INTO support.conversations (browser_session_id) "
                    "VALUES (:s) RETURNING id"
                ),
                {"s": session_id},
            )
        ).scalar_one()


@pytest_asyncio.fixture
async def conversation(db):
    """A browser session plus one conversation row; returns the conversation id."""
    return await make_conversation(db)


def _cid(conversation_or_claim):
    """Helpers accept a conversation id or a Claim (anything with ``conversation_id``)."""
    return getattr(conversation_or_claim, "conversation_id", conversation_or_claim)


async def claim_in_tx(db, conversation_id, turn_id, lease_s: int = 60):
    """Claim in its own transaction (begin -> try_claim -> commit). Returns Claim | None."""
    from support_api.turns.claim import try_claim

    async with db.begin() as conn:
        return await try_claim(conn, conversation_id, turn_id, lease_s)


async def expire_lease(db, conversation_id) -> None:
    from sqlalchemy import text

    conversation_id = _cid(conversation_id)

    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.conversations "
                "SET active_turn_lease_expires_at = now() - interval '1 second' WHERE id = :c"
            ),
            {"c": conversation_id},
        )


async def take_over(db, conversation_id):
    """Expire the current lease and claim as a new turn; returns the new Claim."""
    conversation_id = _cid(conversation_id)
    await expire_lease(db, conversation_id)
    claim = await claim_in_tx(db, conversation_id, uuid.uuid4())
    assert claim is not None
    return claim


class PauseGate:
    """Handle for ``pause_at``: ``reached`` is set when the checkpoint is hit; ``release()`` resumes."""

    def __init__(self, name: str, nth: int) -> None:
        self.reached = asyncio.Event()
        self._release = asyncio.Event()
        gate = self

        class _Paused(Checkpoints):
            def __init__(self) -> None:
                self.hits = 0

            async def hit(self, hit_name: str) -> None:
                if hit_name != name:
                    return
                self.hits += 1
                if self.hits == nth:
                    gate.reached.set()
                    await gate._release.wait()

        self.checkpoints = _Paused()

    def release(self) -> None:
        self._release.set()


@pytest.fixture
def pause_at():
    def make(name: str, *, nth: int = 1) -> PauseGate:
        return PauseGate(name, nth)

    return make


@pytest.fixture
def crash_at():
    def make(name: str, *, nth: int = 1) -> Checkpoints:
        class _Crashing(Checkpoints):
            def __init__(self) -> None:
                self.hits = 0

            async def hit(self, hit_name: str) -> None:
                if hit_name != name:
                    return
                self.hits += 1
                if self.hits == nth:
                    raise SimulatedCrash(f"simulated crash at {name} (hit {nth})")

        return _Crashing()

    return make


# ---------------------------------------------------------------------------
# Identity helpers (Task 9): claimed conversations, attempt slots, counters, assurance.
# Reused by Task 10 (authenticate_customer, tool gating) and Task 17 (turn pipeline).
# ---------------------------------------------------------------------------
HASH = (
    "a" * 64
)  # a stand-in identifier lockout hash (hex HMAC shape); never a real identifier


async def make_claimed_conversation(db):
    """A new conversation with a real ``turns`` row, claimed by that turn. Returns the Claim."""
    from sqlalchemy import text

    conversation_id = await make_conversation(db)
    turn_id = uuid.uuid4()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO support.turns (id, conversation_id, client_message_id, "
                "text_sha256, correlation_id) VALUES (:t, :c, :m, 'x', 'corr')"
            ),
            {"t": turn_id, "c": conversation_id, "m": uuid.uuid4()},
        )
    claim = await claim_in_tx(db, conversation_id, turn_id)
    assert claim is not None
    return claim


@pytest_asyncio.fixture
async def claimed_conversation(db):
    return await make_claimed_conversation(db)


@pytest_asyncio.fixture
async def ten_claimed_conversations(db):
    return list(
        await asyncio.gather(*(make_claimed_conversation(db) for _ in range(10)))
    )


async def admit_in_fenced(
    db, claim, identifier_hash, *, barrier: asyncio.Barrier | None = None
):
    """``admit`` inside its own ``fenced`` transaction, as a turn worker runs it.

    With ``barrier``, every caller holds its fenced transaction (conversation row locked, claim
    verified) and waits there until all parties arrive, so concurrent admissions truly overlap.
    Returns the attempt id; ``AdmissionRejected`` propagates.
    """
    from support_api.identity.attempts import admit
    from support_api.turns.claim import fenced

    async with fenced(db, claim) as conn:
        if barrier is not None:
            await asyncio.wait_for(barrier.wait(), 10)
        return await admit(conn, claim, identifier_hash)


async def fail_attempt(db, claim, identifier_hash=HASH):
    """Admit then record ``failed``; returns the attempt id."""
    from support_api.identity.attempts import record_outcome

    attempt = await admit_in_fenced(db, claim, identifier_hash)
    assert await record_outcome(db, attempt, "failed") is True
    return attempt


@pytest_asyncio.fixture
async def attempt(db, claimed_conversation):
    """An admitted, in-flight attempt slot on ``claimed_conversation``."""
    return await admit_in_fenced(db, claimed_conversation, HASH)


@pytest_asyncio.fixture
async def claimed_conversation_with_two_failures(db, claimed_conversation):
    """Two recorded failures for ``HASH`` on both the identifier and this conversation."""
    await fail_attempt(db, claimed_conversation)
    await fail_attempt(db, claimed_conversation)
    return claimed_conversation


@pytest_asyncio.fixture
async def verified_conversation(db, claimed_conversation):
    """A claimed conversation with kba assurance applied (customer_ref cus_a, first name Ana)."""
    from support_api.identity.attempts import apply_verified

    attempt = await admit_in_fenced(db, claimed_conversation, HASH)
    assert await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana")
    return claimed_conversation


async def counters(db, attempt_id):
    """Failure counters and lock state for the identifier and conversation of ``attempt_id``."""
    from types import SimpleNamespace

    from sqlalchemy import text

    async with db.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT COALESCE(il.failed_count, 0) AS identifier_failed, "
                    "COALESCE(il.locked_until > now(), false) AS identifier_locked, "
                    "COALESCE(ca.failed_attempts, 0) AS conversation_failed, "
                    "COALESCE(ca.locked_until > now(), false) AS conversation_locked "
                    "FROM support.verification_attempts va "
                    "LEFT JOIN support.identifier_lockouts il "
                    "  ON il.identifier_hash = va.identifier_hash "
                    "LEFT JOIN support.conversation_auth ca "
                    "  ON ca.conversation_id = va.conversation_id "
                    "WHERE va.id = :a"
                ),
                {"a": attempt_id},
            )
        ).one()
    return SimpleNamespace(**row._mapping)


async def attempt_status(db, attempt_id) -> str:
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text("SELECT status FROM support.verification_attempts WHERE id = :a"),
                {"a": attempt_id},
            )
        ).scalar_one()


async def assurance(db, conversation_or_claim):
    from support_api.identity.assurance import current_assurance

    async with db.begin() as conn:
        return await current_assurance(conn, _cid(conversation_or_claim))


async def expire_verification(db, conversation_or_claim) -> None:
    """Backdate ``verified_expires_at`` so the verification has just expired."""
    from sqlalchemy import text

    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.conversation_auth "
                "SET verified_expires_at = now() - interval '1 second' "
                "WHERE conversation_id = :c"
            ),
            {"c": _cid(conversation_or_claim)},
        )
