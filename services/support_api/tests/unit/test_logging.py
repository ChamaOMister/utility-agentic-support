import io
import json
import logging as stdlib_logging

import pytest
from support_api import logging as applog
from support_api.logging import ALLOWED_KEYS, RedactionFilter, correlation_id, redact

log = stdlib_logging.getLogger("support_api.test")

SECTION_5_7 = {
    "event", "correlation_id", "conversation_id", "turn_id", "claim_version", "tool_name",
    "status", "error_code", "duration_ms", "http_status", "attempt", "model", "service_tier",
    "input_tokens", "output_tokens", "estimated_cost_usd", "record_count",
}  # fmt: skip


class Captured:
    """Indexable view of the JSON lines the configured handler wrote; ``raw`` is the whole text."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def raw(self) -> str:
        return self._stream.getvalue()

    def __getitem__(self, i: int) -> dict:
        return json.loads(self.raw.splitlines()[i])


@pytest.fixture
def caplog_json():
    stream = io.StringIO()
    handler = applog.configure(stream=stream)
    try:
        yield Captured(stream)
    finally:
        stdlib_logging.getLogger().removeHandler(handler)


def test_allowlist_is_exactly_spec_5_7_plus_envelope():
    assert set(ALLOWED_KEYS) == SECTION_5_7 | {"ts", "level", "logger"}


def test_log_allowlist_drops_payloads(caplog_json):
    log.info(
        "tool_done",
        extra={
            "tool_name": "list_bills",
            "text": "CPF 123.456.789-09",
            "birth_date": "1980-02-01",
        },
    )
    rec = caplog_json[-1]
    assert rec.keys() <= set(ALLOWED_KEYS)
    assert rec["event"] == "tool_done" and rec["tool_name"] == "list_bills"
    assert "123.456" not in caplog_json.raw and "1980" not in caplog_json.raw
    assert {"ts", "level", "logger"} <= rec.keys()
    assert rec["level"] == "INFO" and rec["logger"] == "support_api.test"


def test_every_allowlisted_key_passes_through(caplog_json):
    extra = {
        "conversation_id": "c1", "turn_id": "t1", "claim_version": 3, "tool_name": "x",
        "status": "ok", "error_code": "e", "duration_ms": 12, "http_status": 200,
        "attempt": 2, "model": "m", "service_tier": "default", "input_tokens": 5,
        "output_tokens": 6, "estimated_cost_usd": "0.001", "record_count": 4,
    }  # fmt: skip
    log.info("evt", extra=extra)
    rec = caplog_json[-1]
    for k, v in extra.items():
        assert rec[k] == v


def test_exception_text_and_args_are_not_emitted(caplog_json):
    try:
        raise ValueError("secret payload 123.456.789-09")
    except ValueError:
        log.exception("boom")
    assert (
        "secret payload" not in caplog_json.raw and "Traceback" not in caplog_json.raw
    )
    assert caplog_json[-1]["event"] == "boom"


def test_event_is_redacted(caplog_json):
    log.info("cpf 123.456.789-09 / 12345678909 conta 1234567890 em 01/02/1980")
    ev = caplog_json[-1]["event"]
    for leaked in ("123.456.789-09", "12345678909", "1234567890", "01/02/1980"):
        assert leaked not in ev


def test_event_args_are_redacted(caplog_json):
    log.info("lookup %s", "123.456.789-09")
    assert "123.456" not in caplog_json.raw


def test_correlation_id_contextvar_in_every_record(caplog_json):
    log.info("no_context")
    assert caplog_json[-1].get("correlation_id") is None
    token = correlation_id.set("req-12345678")
    try:
        log.info("with_context")
        stdlib_logging.getLogger("support_api.other").warning("other_logger")
    finally:
        correlation_id.reset(token)
    assert caplog_json[-2]["correlation_id"] == "req-12345678"
    assert caplog_json[-1]["correlation_id"] == "req-12345678"


@pytest.mark.parametrize(
    "raw,expected_gone",
    [
        ("CPF 123.456.789-09", "123.456.789-09"),
        ("CPF 12345678909 ok", "12345678909"),
        ("conta 1234567890", "1234567890"),
        ("nasc 01/02/1980", "01/02/1980"),
        ("nasc 1980-02-01", "1980-02-01"),
        ("key " + "a1" * 16, "a1" * 16),
        ("key " + "AbCdEf0123456789" * 4, "AbCdEf0123456789" * 4),
        ("sk-" + "A" * 24, "A" * 24),
    ],
)
def test_redact_masks_patterns(raw, expected_gone):
    out = redact(raw)
    assert expected_gone not in out and "REDACTED" in out


@pytest.mark.parametrize(
    "benign",
    [
        "tool_done",
        "turn 12 attempt 3",
        "123456789",  # 9 digits
        "123456789012",  # 12 digits: not a CPF / account length
        "6f1c2d3e-0000-4000-8000-123456789abc",  # uuid
        "1980",
    ],
)
def test_redact_leaves_benign_text(benign):
    assert redact(benign) == benign


def test_redaction_filter_rewrites_record():
    rec = stdlib_logging.LogRecord(
        "x", 20, __file__, 1, "cpf %s", ("123.456.789-09",), None
    )
    assert RedactionFilter().filter(rec) is True
    assert "123.456" not in rec.getMessage()


def test_configure_is_idempotent_and_writes_stdout(capsys):
    h1 = applog.configure()
    h2 = applog.configure()
    try:
        ours = [
            h for h in stdlib_logging.getLogger().handlers if getattr(h, "_uas", False)
        ]
        assert ours == [h2] and h1 not in ours
        log.info("hello")
        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(line)["event"] == "hello"
    finally:
        stdlib_logging.getLogger().removeHandler(h2)
