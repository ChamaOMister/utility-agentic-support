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
    await app.state.billing.aclose()
    await app.state.db.engine.dispose()


@pytest_asyncio.fixture
async def client_demo_off(settings):
    app, http = await _client_for(
        settings.model_copy(update={"demo_data_endpoint_enabled": False})
    )
    async with http:
        yield http
    await app.state.billing.aclose()
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
    await app.state.billing.aclose()
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
async def _reset_project_budget(database) -> None:
    """The project budget row is shared by every test: zero its counters (cap is left alone) so
    one test's reservations never leak into another test or the pristine-seed migration test."""
    from sqlalchemy import text

    async with database.begin() as conn:
        await conn.execute(
            text(
                "UPDATE support.budget_accounts "
                "SET spent_usd = 0, reserved_usd = 0, uncertain_usd = 0 WHERE id = 'project'"
            )
        )


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
    await _reset_project_budget(database)
    try:
        yield database
    finally:
        await _reset_project_budget(database)
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
                "SET active_turn_lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = :c"
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


class CheckpointHub(Checkpoints):
    """The app-level checkpoints of the turn API fixtures: forwards every hit to the checkpoints
    registered by ``pause_at`` / ``crash_at`` during the test, in registration order."""

    def __init__(self) -> None:
        self.targets: list[Checkpoints] = []

    async def hit(self, name: str) -> None:
        for target in list(self.targets):
            await target.hit(name)


@pytest.fixture
def checkpoint_hub() -> CheckpointHub:
    return CheckpointHub()


@pytest.fixture
def pause_at(checkpoint_hub):
    """Also registered with ``checkpoint_hub``, so a gate made inside a test reaches the app."""

    def make(name: str, *, nth: int = 1) -> PauseGate:
        gate = PauseGate(name, nth)
        checkpoint_hub.targets.append(gate.checkpoints)
        return gate

    return make


@pytest.fixture
def crash_at(checkpoint_hub):
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

        crashing = _Crashing()
        checkpoint_hub.targets.append(crashing)
        return crashing

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


# ---------------------------------------------------------------------------
# Budget helpers (Task 12): ledger rows, budget rows, reservations. Reused by Tasks 17-18.
# ---------------------------------------------------------------------------
async def lock_conversation(conn, conversation_id) -> None:
    """Take the conversation row lock in the caller's transaction (as recovery's caller does)."""
    from sqlalchemy import text

    await conn.execute(
        text("SELECT 1 FROM support.conversations WHERE id = :c FOR UPDATE"),
        {"c": _cid(conversation_id)},
    )


async def llm_call(db, llm_call_id):
    """The ``llm_calls`` row (as a row object with column attributes)."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM support.llm_calls WHERE id = :i"),
                {"i": llm_call_id},
            )
        ).one()


async def conv_budget(db, conversation_or_claim):
    """The ``conversation_budgets`` row, or ``None`` if none was created yet."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.conversation_budgets WHERE conversation_id = :c"
                ),
                {"c": _cid(conversation_or_claim)},
            )
        ).one_or_none()


async def project_budget(db):
    """The ``budget_accounts`` row for the project. It is shared by every test: assert on deltas."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM support.budget_accounts WHERE id = 'project'")
            )
        ).one()


async def conversation_row(db, conversation_or_claim):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM support.conversations WHERE id = :c"),
                {"c": _cid(conversation_or_claim)},
            )
        ).one()


async def reserve_in_fenced(
    db, claim, amount, *, barrier: asyncio.Barrier | None = None, **overrides
):
    """``reserve`` inside its own ``fenced`` transaction, as a turn worker runs it.

    With ``barrier``, every caller waits (bounded) until all parties have started. Returns the
    llm_call id; ``BudgetExceeded`` / ``ClaimLost`` propagate.
    """
    from support_api.turns.claim import fenced
    from support_api.usage.budget import reserve

    if barrier is not None:
        await asyncio.wait_for(barrier.wait(), 10)
    args = {"kind": "generation", "model": "m", "tier": "default", "attempt": 1}
    args.update(overrides)
    async with fenced(db, claim) as conn:
        return await reserve(conn, claim, amount=amount, **args)


@pytest_asyncio.fixture
async def reservation(db, claimed_conversation):
    """A ``reserved`` generation call of 0.04 on ``claimed_conversation`` (``.id``, ``.claim``)."""
    from decimal import Decimal
    from types import SimpleNamespace

    amount = Decimal("0.04")
    llm_call_id = await reserve_in_fenced(db, claimed_conversation, amount)
    return SimpleNamespace(id=llm_call_id, claim=claimed_conversation, amount=amount)


@pytest_asyncio.fixture
async def many_claimed(ten_claimed_conversations):
    return ten_claimed_conversations


@pytest_asyncio.fixture
async def project_cap_small(db):
    """Lower the project cap to 0.05 above what is already held/spent; restores it afterwards.

    Yields the headroom (``Decimal("0.05")``). The project row is shared across tests, so the cap is
    relative to the current total rather than absolute.
    """
    from decimal import Decimal

    from sqlalchemy import text

    headroom = Decimal("0.05")
    async with db.begin() as conn:
        original = (
            await conn.execute(
                text(
                    "SELECT cap_usd FROM support.budget_accounts WHERE id = 'project' FOR UPDATE"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE support.budget_accounts SET "
                "cap_usd = spent_usd + reserved_usd + uncertain_usd + :h WHERE id = 'project'"
            ),
            {"h": headroom},
        )
    try:
        yield headroom
    finally:
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE support.budget_accounts SET cap_usd = :c WHERE id = 'project'"
                ),
                {"c": original},
            )


# ---------------------------------------------------------------------------
# Turn API (Task 17): app + client + one conversation, billing mocked with respx, model control.
# ---------------------------------------------------------------------------
TEST_HASH_KEY = (
    b"test-hash-key"  # the identifier_hash_key of the test settings, as bytes
)
BILLING_TEST_URL = "http://billing.test"  # the billing_base_url of the test settings
_BILL_SUMMARY_FIELDS = (
    "bill_id", "installation_id", "reference_month", "due_date", "amount_brl", "status",
    "consumption_kwh", "tariff_flag",
)  # fmt: skip


def persona_hash(persona: dict) -> str:
    """The identifier lockout key the executor stores for ``persona``'s CPF."""
    from support_api.identity.normalize import lockout_key, normalize

    normalized = normalize(persona["cpf"])
    assert normalized is not None
    return lockout_key(normalized, TEST_HASH_KEY)


def persona_creds_text(persona: dict) -> str:
    """A message carrying ``persona``'s credentials, as a customer would type it."""
    from support_api.agent.format_en import MONTHS

    cpf = persona["cpf"]
    y, m, d = (int(part) for part in persona["birth_date"].split("-"))
    formatted = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    born = f"{d} {MONTHS[m - 1]} {y}"
    return f"CPF {formatted}, born {born}, phone ending {persona['phone'][-4:]}"


def fake_call(call_id: str, name: str, args: dict | None = None):
    """A ModelResponse with one function call (QueuedFakeModel queue entry)."""
    from support_api.agent.models.base import FunctionCall, ModelResponse

    arguments = json.dumps(args or {})
    item = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }
    return ModelResponse(
        "completed", None, (FunctionCall(call_id, name, arguments),), (item,)
    )


def fake_text(reply: str):
    """A ModelResponse with final assistant text (QueuedFakeModel queue entry)."""
    from support_api.agent.models.base import ModelResponse

    msg = {"type": "message", "role": "assistant", "content": reply}
    return ModelResponse("completed", reply, (), (msg,))


class BillingGate:
    """Holds the verify route: ``hold()`` makes the next verify requests wait for ``release()``.

    ``request_seen`` is set when a held request arrives. ``hold(response=...)`` replaces the JSON
    the held request answers with (default: the route's normal persona check).
    """

    def __init__(self) -> None:
        self.held = False
        self.response: dict | None = None
        self.request_seen = asyncio.Event()
        self._release = asyncio.Event()

    def hold(self, response: dict | None = None) -> None:
        self.held = True
        self.response = response

    def release(self) -> None:
        self._release.set()

    async def wait(self) -> None:
        self.request_seen.set()
        await asyncio.wait_for(self._release.wait(), 10)


@pytest.fixture
def billing_gate() -> BillingGate:
    return BillingGate()


@pytest.fixture
def billing_mock(personas, billing_gate):
    """respx routes for the billing contract on ``BILLING_TEST_URL``, answering from the personas.

    Route names: ``verify``, ``list_bills``, ``get_account``.
    """
    import respx

    by_ref = {p["customer_ref"]: p for p in personas}

    def check(body: dict) -> dict:
        for p in personas:
            if (
                body.get("identifier") in (p["cpf"], p["account_number"])
                and body.get("birth_date") == p["birth_date"]
                and body.get("phone_last4") == p["phone"][-4:]
            ):
                return {
                    "verified": True,
                    "customer_ref": p["customer_ref"],
                    "first_name": p["first_name"],
                }
        return {"verified": False}

    async def verify(request):
        body = json.loads(request.content)
        if billing_gate.held:
            await billing_gate.wait()
            if billing_gate.response is not None:
                return httpx.Response(200, json=billing_gate.response)
        return httpx.Response(200, json=check(body))

    def list_bills(request, ref):
        persona = by_ref.get(ref)
        if persona is None:
            return httpx.Response(404, json={"error": "not_found"})
        status = request.url.params.get("status")
        bills = [
            {k: b[k] for k in _BILL_SUMMARY_FIELDS}
            for b in persona["bills"]
            if status is None or b["status"] == status
        ]
        return httpx.Response(200, json=bills)

    def get_account(request, ref):
        persona = by_ref.get(ref)
        if persona is None:
            return httpx.Response(404, json={"error": "not_found"})
        cpf = persona["cpf"]
        return httpx.Response(
            200,
            json={
                "customer_ref": ref,
                "masked_cpf": f"***.{cpf[3:6]}.{cpf[6:9]}-**",
                "installations": persona["installations"],
                "address": persona["address"],
                "tariff_class": persona["tariff_class"],
                "account_status": persona["account_status"],
            },
        )

    with respx.mock(base_url=BILLING_TEST_URL, assert_all_called=False) as router:
        router.post("/v1/identity/verify", name="verify").mock(side_effect=verify)
        router.get(
            path__regex=r"^/v1/customers/(?P<ref>[A-Za-z0-9_-]+)/bills$",
            name="list_bills",
        ).mock(side_effect=list_bills)
        router.get(
            path__regex=r"^/v1/customers/(?P<ref>[A-Za-z0-9_-]+)/account$",
            name="get_account",
        ).mock(side_effect=get_account)
        yield router


class SlowGate:
    """Makes the counting scripted model wait inside ``generate`` until ``release()``."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await asyncio.wait_for(self._release.wait(), 10)

    def release(self) -> None:
        self._release.set()


class CountingScriptedModel:
    """``ScriptedModel`` that counts generation requests and can be held by a ``SlowGate``."""

    name = "scripted"
    live = False

    def __init__(self) -> None:
        from support_api.agent.models.scripted import ScriptedModel

        self._inner = ScriptedModel()
        self.requests_count = 0
        self.slow: SlowGate | None = None

    async def count_input_tokens(self, request, ctx) -> int:
        return await self._inner.count_input_tokens(request, ctx)

    async def generate(self, request, ctx):
        self.requests_count += 1
        if self.slow is not None:
            await self.slow.wait()
        return await self._inner.generate(request, ctx)


class TurnApi:
    """An httpx client (own cookie jar) bound to the app, plus one conversation it owns."""

    def __init__(self, app, http, conversation_id, model, billing_routes) -> None:
        self.app = app
        self.http = http
        self.conversation_id = conversation_id
        self.model = model
        self.billing_routes = billing_routes

    async def send(self, mid, text: str) -> httpx.Response:
        return await self.http.post(
            f"/api/conversations/{self.conversation_id}/messages",
            json={"client_message_id": str(mid), "text": text},
            headers=ORIGIN,
        )

    async def transcript(self) -> httpx.Response:
        return await self.http.get(f"/api/conversations/{self.conversation_id}")


async def _turn_api(settings, model, checkpoint_hub, billing_mock):
    from support_api.app import create_app

    app = create_app(settings, model=model, checkpoints=checkpoint_hub)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://support.test"
    )
    r = await http.post("/api/conversations", json={}, headers=ORIGIN)
    assert r.status_code == 201
    cid = uuid.UUID(r.json()["conversation_id"])
    return TurnApi(app, http, cid, model, billing_mock)


async def _close_turn_api(api: TurnApi) -> None:
    await api.http.aclose()
    await api.app.state.billing.aclose()
    await api.app.state.db.dispose()


@pytest_asyncio.fixture
async def api(db, settings, checkpoint_hub, billing_mock):
    """Turn API with the (counting) scripted model and respx billing."""
    api = await _turn_api(
        settings, CountingScriptedModel(), checkpoint_hub, billing_mock
    )
    yield api
    await _close_turn_api(api)


@pytest.fixture
def slow_model(api) -> SlowGate:
    """Holds ``api``'s model inside its first ``generate`` until ``release()``."""
    gate = SlowGate()
    api.model.slow = gate
    return gate


@pytest_asyncio.fixture
async def api_fake_model(db, settings, checkpoint_hub, billing_mock):
    """Turn API with an empty ``QueuedFakeModel``; tests ``push`` the responses each turn needs."""
    from support_api.agent.models.fake import QueuedFakeModel

    api = await _turn_api(settings, QueuedFakeModel([]), checkpoint_hub, billing_mock)
    yield api
    await _close_turn_api(api)


@pytest_asyncio.fixture
async def api_verified_fake_model(api_fake_model, db, personas):
    """``api_fake_model`` whose conversation is already verified as persona A (kba, 30 min)."""
    from sqlalchemy import text

    a = personas[0]
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO support.conversation_auth "
                "(conversation_id, assurance_level, customer_ref, first_name, verified_at, "
                " verified_expires_at) "
                "VALUES (:c, 'kba', :r, :n, now(), now() + interval '30 minutes')"
            ),
            {
                "c": api_fake_model.conversation_id,
                "r": a["customer_ref"],
                "n": a["first_name"],
            },
        )
    return api_fake_model


# Turn DB helpers (Task 17).
def turn_id(response: httpx.Response) -> uuid.UUID:
    return uuid.UUID(response.json()["turn_id"])


async def turns(db, conversation_id):
    """Turn rows of the conversation, oldest first."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.turns WHERE conversation_id = :c "
                    "ORDER BY created_at, id"
                ),
                {"c": _cid(conversation_id)},
            )
        ).all()


async def turn_by_mid(db, client_message_id):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM support.turns WHERE client_message_id = :m"),
                {"m": client_message_id},
            )
        ).one()


async def items_for(db, turn_id) -> list[str]:
    """Item kinds of one turn, in ``seq`` order."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT kind FROM support.conversation_items "
                        "WHERE turn_id = :t ORDER BY seq"
                    ),
                    {"t": turn_id},
                )
            ).scalars()
        )


async def item_turn_ids(db, conversation_id) -> set:
    """Every turn id that owns at least one item in the conversation."""
    from sqlalchemy import text

    async with db.begin() as conn:
        return set(
            (
                await conn.execute(
                    text(
                        "SELECT DISTINCT turn_id FROM support.conversation_items "
                        "WHERE conversation_id = :c"
                    ),
                    {"c": _cid(conversation_id)},
                )
            ).scalars()
        )


async def conv_auth(db, conversation_id):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.conversation_auth WHERE conversation_id = :c"
                ),
                {"c": _cid(conversation_id)},
            )
        ).one()


async def last_attempt(db, conversation_id):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.verification_attempts WHERE conversation_id = :c "
                    "ORDER BY created_at DESC, id LIMIT 1"
                ),
                {"c": _cid(conversation_id)},
            )
        ).one()


async def identifier_row(db, identifier_hash):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.identifier_lockouts WHERE identifier_hash = :h"
                ),
                {"h": identifier_hash},
            )
        ).one()


# OpenAI fixtures use the real SDK with legacy httpx, intercepted by respx. Every key is fake.
def provider_response(
    *, output=None, status="completed", service_tier="default", usage=True
):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": status,
        "service_tier": service_tier,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": True,
        "tools": [],
        "tool_choice": "auto",
        "temperature": 1,
        "top_p": 1,
        "output": output
        if output is not None
        else [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Certo!", "annotations": []}
                ],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 110,
        }
        if usage
        else None,
    }


class OpenAIMock:
    def __init__(self, router):
        self.routes = {
            name: router.post("https://api.openai.com/v1/" + path, name=name)
            for name, path in (
                ("input_tokens", "responses/input_tokens"),
                ("responses", "responses"),
            )
        }
        self.routes["input_tokens"].respond(
            200, json={"object": "response.input_tokens", "input_tokens": 100}
        )
        self.routes["responses"].respond(
            200, json=provider_response(), headers={"x-request-id": "req_test"}
        )

    def route(self, name):
        return self.routes[name]

    @property
    def call_count(self):
        return sum(r.call_count for r in self.routes.values())

    def captured_all(self, name):
        return [json.loads(c.request.content) for c in self.routes[name].calls]

    def count_returns(self, values):
        self.routes["input_tokens"].mock(
            side_effect=[
                httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": n}
                )
                for n in values
            ]
        )


@pytest.fixture
def openai_mock(billing_mock):
    return OpenAIMock(billing_mock)


@pytest.fixture
def verified_live_config(monkeypatch):
    from support_api.usage.pricing import ModelsConfig, Pricing

    fixtures = SERVICE_DIR / "tests" / "fixtures"
    pricing_data = json.loads((fixtures / "pricing_verified.json").read_text())
    model_data = json.loads((fixtures / "models_verified.json").read_text())
    pricing = Pricing(pricing_data["models"], pricing_data["token_count"])
    models = ModelsConfig(model_data)
    monkeypatch.setattr(Pricing, "load", lambda path: pricing)
    monkeypatch.setattr(ModelsConfig, "load", lambda path: models)
    return pricing_data, model_data, pricing, models


@pytest.fixture
def live_settings(settings, verified_live_config):
    from pydantic import SecretStr

    return settings.model_copy(
        update={
            "llm_provider": "openai",
            "live_llm_enabled": True,
            "openai_api_key": SecretStr("sk-test-not-real"),
            "openai_chat_model": "gpt-6-luna",
            "openai_service_tier": "default",
        }
    )


@pytest_asyncio.fixture
async def live_app_factory(
    db, live_settings, verified_live_config, checkpoint_hub, openai_mock, fake_clock
):
    from support_api.app import create_app

    apis = []

    async def make(*, reasoning_items="current_turn", **overrides):
        verified_live_config[1]["gpt-6-luna"]["reasoning_items"] = reasoning_items
        app = create_app(
            live_settings.model_copy(update=overrides),
            checkpoints=checkpoint_hub,
            clock=fake_clock,
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://support.test"
        )
        response = await http.post("/api/conversations", json={}, headers=ORIGIN)
        assert response.status_code == 201
        api = TurnApi(
            app,
            http,
            uuid.UUID(response.json()["conversation_id"]),
            app.state.model,
            None,
        )
        api.openai_routes = openai_mock.routes
        apis.append(api)
        return api

    yield make
    for api in apis:
        await api.model.aclose()
        await _close_turn_api(api)


@pytest_asyncio.fixture
async def api_live_mocked(live_app_factory):
    return await live_app_factory()


async def llm_calls(db, conversation_id, *, kind="generation"):
    from sqlalchemy import text

    async with db.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM support.llm_calls WHERE conversation_id = :c AND kind = :k ORDER BY created_at, id"
                ),
                {"c": _cid(conversation_id), "k": kind},
            )
        ).all()


async def only_llm_call(db, conversation_id, *, kind="generation"):
    rows = await llm_calls(db, conversation_id, kind=kind)
    assert len(rows) == 1
    return rows[0]


async def live_blocked_reason(db, conversation_id):
    return (await conversation_row(db, conversation_id)).live_blocked_reason


# Task 20: full offline API with a real fault-enabled billing subprocess.
class ObservedBillingTransport(httpx.AsyncBaseTransport):
    """Real HTTP transport; count outbound verify requests without retaining payloads."""

    def __init__(self, verify_response_delay=0):
        self.inner = httpx.AsyncHTTPTransport()
        self.verify_response_delay = verify_response_delay
        self.verify_requests = []

    async def handle_async_request(self, request):
        verifying = request.url.path == "/v1/identity/verify"
        if verifying:
            self.verify_requests.append(request.method)
        response = await self.inner.handle_async_request(request)
        if verifying and self.verify_response_delay:
            await asyncio.sleep(self.verify_response_delay)
        return response

    async def aclose(self):
        await self.inner.aclose()


@pytest_asyncio.fixture
async def live_billing_api_factory(db, settings, billing_server, checkpoint_hub):
    """Each API has its own browser session; all use the real billing process and shared DB."""
    from support_api.app import create_app

    apis = []

    async def make(*, verify_response_delay=0):
        transport = ObservedBillingTransport(verify_response_delay)
        app = create_app(
            settings.model_copy(update={"billing_base_url": billing_server.base_url}),
            billing_transport=transport,
            checkpoints=checkpoint_hub,
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://support.test"
        )
        response = await http.post("/api/conversations", json={}, headers=ORIGIN)
        assert response.status_code == 201
        api = TurnApi(
            app,
            http,
            uuid.UUID(response.json()["conversation_id"]),
            app.state.model,
            transport,
        )
        apis.append(api)
        return api

    try:
        yield make
    finally:
        for api in apis:
            await _close_turn_api(api)


@pytest_asyncio.fixture
async def set_fault(billing_server):
    """Replace/clear authenticated dev fault rules; clean up even after a failing assertion."""
    async with httpx.AsyncClient(base_url=billing_server.base_url, timeout=3) as http:
        headers = {"X-Fault-Admin-Key": billing_server.fault_admin_key}

        async def put(route_name=None, customer_ref=None, effect=None):
            if route_name is None:
                response = await http.delete("/_dev/faults", headers=headers)
            else:
                response = await http.put(
                    "/_dev/faults",
                    headers=headers,
                    json={
                        "rules": [
                            {
                                "route_name": route_name,
                                "customer_ref": customer_ref,
                                "effect": effect,
                            }
                        ]
                    },
                )
            assert response.status_code == 204

        await put()
        try:
            yield put
        finally:
            await put()


@pytest.fixture
def caplog_json():
    """Capture the production formatter/filter synchronously while request context is active."""
    import io
    import logging

    from support_api.logging import configure

    stream = io.StringIO()
    root = logging.getLogger()
    original_level = root.level
    handler = configure(stream=stream)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)
