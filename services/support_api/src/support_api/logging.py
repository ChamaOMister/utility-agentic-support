"""JSON logs to stdout, restricted to an allowlist of metadata fields (spec §5.7).

Payloads are never logged: anything not in ``ALLOWED_KEYS`` is dropped by the formatter, including
``exc_info``/tracebacks. ``RedactionFilter`` is defense-in-depth for ``event`` only.
"""

import json
import logging
import re
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TextIO
from uuid import uuid4

# Spec §5.7 allowlist, verbatim, plus the envelope keys the formatter always writes.
SPEC_ALLOWLIST = (
    "event", "correlation_id", "conversation_id", "turn_id", "claim_version", "tool_name",
    "status", "error_code", "duration_ms", "http_status", "attempt", "model", "service_tier",
    "input_tokens", "output_tokens", "estimated_cost_usd", "record_count",
)  # fmt: skip
ENVELOPE_KEYS = ("ts", "level", "logger")
ALLOWED_KEYS = frozenset(SPEC_ALLOWLIST) | frozenset(ENVELOPE_KEYS)

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_REDACTIONS = (
    (re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"), "[REDACTED:cpf]"),
    (re.compile(r"(?<!\d)\d{2}/\d{2}/\d{4}(?!\d)"), "[REDACTED:date]"),
    (re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"), "[REDACTED:date]"),
    (re.compile(r"(?<!\d)\d{11}(?!\d)"), "[REDACTED:cpf]"),
    (re.compile(r"(?<!\d)\d{10}(?!\d)"), "[REDACTED:account]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED:key]"),
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])"), "[REDACTED:key]"),
)  # fmt: skip


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Masks CPF, account number, date and key-like patterns in the event text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        explicit = record.__dict__.get("event")
        if isinstance(explicit, str):
            record.__dict__["event"] = redact(explicit)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
        }
        for key in SPEC_ALLOWLIST:
            if key in record.__dict__:
                out[key] = record.__dict__[key]
        if not isinstance(out.get("event"), str):
            out["event"] = record.getMessage()
        if out.get("correlation_id") is None:
            current = correlation_id.get()
            if current is not None:
                out["correlation_id"] = current
            else:
                out.pop("correlation_id", None)
        return json.dumps(out, default=str, ensure_ascii=False)


def configure(stream: TextIO | None = None) -> logging.Handler:
    """Install the JSON handler on the root logger (idempotent) and return it."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, "_uas", False)]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler._uas = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn installs its own plain-text handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    return handler


_CORRELATION_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
_request_log = logging.getLogger("support_api.request")


class CorrelationIdMiddleware:
    """Accept a well-formed ``X-Correlation-ID`` or generate a uuid4; echo it; expose via contextvar.

    Pure ASGI (not BaseHTTPMiddleware) so the contextvar is visible to route handlers.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for name, value in scope["headers"]:
            if name == b"x-correlation-id":
                incoming = value.decode("latin-1")
                break
        cid = incoming if _CORRELATION_RE.fullmatch(incoming) else str(uuid4())
        token = correlation_id.set(cid)
        started = time.perf_counter()
        status = 500

        async def send_with_header(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-correlation-id", cid.encode("ascii"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_log.info(
                "request_done",
                extra={
                    "http_status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            correlation_id.reset(token)
