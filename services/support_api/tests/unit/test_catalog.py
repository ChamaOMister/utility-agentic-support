import pytest
from support_api.catalog import MESSAGES, message

REQUIRED = {
    "auth_failed",
    "verification_expired",
    "upstream_unavailable",
    "limit_reached",
    "live_blocked",
    "turn_in_progress",
    "turn_interrupted",
    "offline_badge",
}


def test_catalog_has_all_keys():
    assert set(MESSAGES) >= REQUIRED


def test_message_lookup_and_unknown_key():
    assert message("offline_badge") == "Modo offline — respostas simuladas"
    with pytest.raises(KeyError):
        message("nope")


def test_config_placeholders_keep_live_gate_closed():
    import json

    from tests.conftest import SERVICE_DIR

    pricing = json.loads((SERVICE_DIR / "config" / "pricing.json").read_text())
    models = json.loads((SERVICE_DIR / "config" / "models.json").read_text())
    assert pricing["models"]["gpt-6-luna"]["default"]["verified"] is False
    assert pricing["token_count"]["billed"] is None
    assert (
        models["gpt-6-luna"]["verified"] is False
        and models["gpt-6-luna"]["reasoning_items"] is None
    )


def test_web_fallback_snapshot_matches_the_central_catalog():
    import json

    from support_api.catalog import UI_MESSAGES

    from tests.conftest import REPO_ROOT

    snapshot = json.loads((REPO_ROOT / "web/src/ui-text.json").read_text())
    assert snapshot == {**UI_MESSAGES, "offline_badge": MESSAGES["offline_badge"]}
