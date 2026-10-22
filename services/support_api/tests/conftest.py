"""Shared test fixtures for support_api (extended by later tasks)."""

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]


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
