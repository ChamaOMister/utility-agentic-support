import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONAS_PATH = REPO_ROOT / "data" / "synthetic" / "personas.json"


@pytest.fixture(scope="session")
def personas() -> list[dict]:
    return json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
