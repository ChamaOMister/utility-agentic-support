import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import ApiError
from .db import make_engine
from .routes import router
from .settings import Settings

logger = logging.getLogger("mock_billing")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def create_app(settings: Settings) -> FastAPI:
    engine = make_engine(settings.billing_database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await engine.dispose()

    app = FastAPI(title="Mock billing (synthetic data)", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        # One fixed body per error kind, so a foreign bill and a missing one are byte-identical.
        return JSONResponse({"error": exc.error}, status_code=exc.status_code)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        supplied = request.headers.get("x-correlation-id", "")
        correlation_id = supplied if _CORRELATION_RE.fullmatch(supplied) else "-"
        started = time.monotonic()
        response = await call_next(request)
        route = request.scope.get("route")
        # Allowlisted fields only: never payloads, identifiers or path parameters.
        logger.info(
            "request handled correlation_id=%s method=%s route=%s status=%d duration_ms=%d",
            correlation_id,
            request.method,
            getattr(route, "path", "-"),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    return app
