import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import openai
from fastapi import FastAPI

from . import sessions
from .agent.core import AgentCore
from .agent.models.base import ChatModel
from .agent.models.openai_model import OpenAIResponsesModel
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
from .usage.pricing import LiveGateClosed, ModelsConfig, Pricing, live_gate_failures

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def create_app(
    settings: Settings,
    *,
    model: ChatModel | None = None,
    billing_transport=None,
    checkpoints: Checkpoints | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    pricing = None
    reasoning_mode = "current_turn"
    if settings.llm_provider == "openai":
        # The container installs this package into site-packages and supplies /app/config.
        config_dir = Path("config") if Path("config").is_dir() else _CONFIG_DIR
        pricing = Pricing.load(config_dir / "pricing.json")
        models = ModelsConfig.load(config_dir / "models.json")
        failures = live_gate_failures(settings, pricing, models)
        if failures:
            raise LiveGateClosed(failures)  # before creating clients or DB resources
        reasoning_mode = models.reasoning_items(settings.openai_chat_model)
    db = Database(settings.support_database_url)
    owned_model = None
    if model is not None:
        chat_model = model
    elif pricing is not None:
        client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=0,
            http_client=httpx.AsyncClient(),
        )
        chat_model = owned_model = OpenAIResponsesModel(
            client,
            db=db,
            model=settings.openai_chat_model,
            tier=settings.openai_service_tier,
            rate=pricing.rate(settings.openai_chat_model, settings.openai_service_tier),
            max_output_tokens=settings.max_output_tokens,
            count_timeout_s=settings.token_count_s,
            attempt_timeout_s=settings.llm_attempt_s,
            conversation_cap_usd=settings.conversation_budget_usd,
            pricing=pricing,
        )
    else:
        chat_model = ScriptedModel()
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
    core = AgentCore(
        db, chat_model, executor, settings, checkpoints, reasoning_mode=reasoning_mode
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await startup_sweep(db)  # recover turns abandoned by a previous process
            yield
        finally:
            if owned_model is not None:
                await owned_model.aclose()
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
