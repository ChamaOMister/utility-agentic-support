"""Shared test fixtures for support_api (extended by later tasks)."""

import functools
import json
import os
import socket
from pathlib import Path

import httpcore
import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_DIR = REPO_ROOT / "services" / "support_api"
PERSONAS_PATH = REPO_ROOT / "data" / "synthetic" / "personas.json"


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
