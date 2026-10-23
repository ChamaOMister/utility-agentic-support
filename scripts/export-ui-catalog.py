"""Regenerate the bundled startup/offline fallback from the central English catalog.

Run with .venv/bin/python scripts/export-ui-catalog.py after editing UI_MESSAGES.
The parity test prevents a stale snapshot from passing the backend suite.
"""
import json
from pathlib import Path

from support_api.catalog import MESSAGES, UI_MESSAGES

root = Path(__file__).resolve().parents[1]
(root / 'web/src/ui-text.json').write_text(
    json.dumps({**UI_MESSAGES, 'offline_badge': MESSAGES['offline_badge']}, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
