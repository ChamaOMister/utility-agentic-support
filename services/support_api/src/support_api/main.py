"""uvicorn entry point: `uvicorn support_api.main:build --factory`."""

import logging

from fastapi import FastAPI

from .app import create_app
from .settings import Settings


def build() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    return create_app(Settings())
