"""uvicorn entry point: `uvicorn support_api.main:build --factory`."""

from fastapi import FastAPI

from .app import create_app
from .logging import configure as configure_logging
from .settings import Settings


def build() -> FastAPI:
    configure_logging()
    return create_app(Settings())
