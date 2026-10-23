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


def test_shipped_defaults_keep_live_mode_off():
    # The provider config is verified; the human switches (flag, key) stay off by default.
    from support_api.settings import Settings

    fields = Settings.model_fields
    assert fields["llm_provider"].default == "scripted"
    assert fields["live_llm_enabled"].default is False
    assert fields["openai_api_key"].default is None


def test_web_fallback_snapshot_matches_the_central_catalog():
    import json

    from support_api.catalog import UI_MESSAGES

    from tests.conftest import REPO_ROOT

    snapshot = json.loads((REPO_ROOT / "web/src/ui-text.json").read_text())
    assert snapshot == {**UI_MESSAGES, "offline_badge": MESSAGES["offline_badge"]}
