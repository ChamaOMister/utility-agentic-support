import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import sessions
from .channels.chat import router as chat_router
from .checkpoints import Checkpoints
from .db import Database
from .demo import load_demo_customers
from .demo import router as demo_router
from .logging import CorrelationIdMiddleware
from .settings import Settings


def create_app(
    settings: Settings,
    *,
    model=None,
    billing_transport=None,
    checkpoints: Checkpoints | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    db = Database(settings.support_database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await db.dispose()

    app = FastAPI(title="Support API (synthetic demo)", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.model = model
    app.state.billing_transport = billing_transport
    app.state.checkpoints = checkpoints or Checkpoints()
    app.state.clock = clock
    sessions.install(app)
    app.include_router(chat_router)
    # Outermost: every response (including errors) carries X-Correlation-ID.
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/api/health")
    async def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "mode": settings.llm_provider,
            "live_enabled": settings.live_llm_enabled,
        }

    if settings.demo_data_endpoint_enabled:
        app.state.demo_customers = load_demo_customers(settings.personas_path)
        app.include_router(demo_router)
    return app
