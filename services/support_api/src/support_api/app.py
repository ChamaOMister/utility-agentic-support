import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import sessions
from .agent.core import AgentCore
from .agent.models.base import ChatModel
from .agent.models.scripted import ScriptedModel
from .channels.chat import router as chat_router
from .checkpoints import Checkpoints
from .db import Database
from .demo import load_demo_customers
from .demo import router as demo_router
from .identity.attempts import IdentityLimits
from .integrations.billing.client import BillingClient
from .logging import CorrelationIdMiddleware
from .settings import Settings
from .tools.executor import Executor
from .turns.recovery import startup_sweep


def _select_model(settings: Settings, model: ChatModel | None) -> ChatModel:
    if model is not None:
        return model
    if settings.llm_provider == "scripted":
        return ScriptedModel()
    raise RuntimeError(
        "LLM_PROVIDER=openai is not available yet: OpenAIResponsesModel arrives in Task 18. "
        "Use LLM_PROVIDER=scripted."
    )


def create_app(
    settings: Settings,
    *,
    model: ChatModel | None = None,
    billing_transport=None,
    checkpoints: Checkpoints | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    chat_model = _select_model(settings, model)  # fails before any resource is created
    db = Database(settings.support_database_url)
    checkpoints = checkpoints or Checkpoints()
    billing = BillingClient(
        settings.billing_base_url,
        settings.billing_api_key.get_secret_value(),
        transport=billing_transport,
    )
    executor = Executor(
        db,
        billing,
        settings.identifier_hash_key.get_secret_value().encode("utf-8"),
        limits=IdentityLimits.from_settings(settings),
    )
    core = AgentCore(db, chat_model, executor, settings, checkpoints)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await startup_sweep(db)  # recover turns abandoned by a previous process
        yield
        await billing.aclose()
        await db.dispose()

    app = FastAPI(title="Support API (synthetic demo)", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.model = chat_model
    app.state.billing_transport = billing_transport
    app.state.billing = billing
    app.state.executor = executor
    app.state.core = core
    app.state.checkpoints = checkpoints
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
