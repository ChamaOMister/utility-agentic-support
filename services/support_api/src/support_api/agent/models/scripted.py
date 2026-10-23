"""Deterministic, rule-based English model for the offline demo (spec §5.6).

It reads only ``request.input`` and never sends anything anywhere, token counts included. Its tool
calls go through the real executor and billing client; its text is built from fixed English templates
plus structured tool data. Tool output is ``untrusted_data``: this module interpolates only fields it
has parsed into numbers, dates or members of fixed vocabularies, plus a sanitised ``first_name``.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from support_api.agent.format_en import MONTHS, format_brl, format_date, format_month
from support_api.agent.history import Item, estimate_tokens
from support_api.agent.models.base import (
    FunctionCall,
    ModelContext,
    ModelRequest,
    ModelResponse,
)
from support_api.catalog import MESSAGES

# --------------------------------------------------------------------------- fixed texts

ASK_VERIFICATION = (
    "To look up your account details, I need to verify your identity. Tap “Verify my identity” "
    "below the message box, or type your CPF or account number, your date of birth and the last "
    "4 digits of your phone number."
)
GREETING = (
    "Hello! I'm the virtual assistant for Vértice Energia (a fictional company, demo project). "
    "I can help you check your open, overdue or paid bills. How can I help?"
)
BILL_NOT_FOUND = "I couldn't find that bill. Please check the bill ID and try again."
_EMPTY_BILLS = {
    "open": "I didn't find any open bills.",
    "overdue": "I didn't find any overdue bills.",
    "paid": "I didn't find any paid bills.",
    None: "I didn't find any bills on your account.",
}
_BILL_STATUS_EN = {"open": "open", "overdue": "overdue", "paid": "paid"}
_ACCOUNT_STATUS_EN = {"active": "active"}
_PICK_FROM_CALENDAR = (
    "tap “Verify my identity” to pick your date of birth from a calendar"
)
_ERROR_TEXT = {
    "authentication_failed": MESSAGES["auth_failed"],
    "locked": MESSAGES["auth_failed"],
    "verification_busy": MESSAGES["auth_failed"],
    "upstream_timeout": MESSAGES["upstream_unavailable"],
    "upstream_unavailable": MESSAGES["upstream_unavailable"],
    "internal_error": MESSAGES["upstream_unavailable"],
    "authentication_required": ASK_VERIFICATION,
    "not_found": BILL_NOT_FOUND,
}

# --------------------------------------------------------------------------- text analysis


def _fold(text: str) -> str:
    """Case- and accent-insensitive form used for every pattern match."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


_INTENT = re.compile(
    r"\b(?:bills?|invoices?|account|payments?|pay|due|balance|statements?)\b"
)
_STATUS_PATTERNS: dict[str, re.Pattern[str]] = {
    "open": re.compile(r"\b(?:open|unpaid|outstanding|pending)\b"),
    "overdue": re.compile(r"\b(?:overdue|late|past[\s-]due)\b"),
    "paid": re.compile(r"\b(?:paid|settled)\b"),
}


def bill_status(text: str) -> str | None:
    """The requested bill status, only when exactly one category matches."""
    folded = _fold(text)
    found = [s for s, p in _STATUS_PATTERNS.items() if p.search(folded)]
    return found[0] if len(found) == 1 else None


_MONTH_WORD = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ORDINAL = r"(?:st|nd|rd|th)?"
# (pattern, order of the year/month/day groups); "M" is a month word, "numeric" is dd/mm or mm/dd.
_DATE_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![\d.])(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"), "ymd"),
    (
        re.compile(
            rf"(?<!\d)(\d{{1,2}}){_ORDINAL}\s+(?:of\s+)?{_MONTH_WORD},?\s+(\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
        "dMy",
    ),
    (
        re.compile(
            rf"\b{_MONTH_WORD}\s+(\d{{1,2}}){_ORDINAL},?\s+(\d{{4}})(?!\d)",
            re.IGNORECASE,
        ),
        "Mdy",
    ),
    (re.compile(r"(?<![\d.])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)"), "numeric"),
)
_MONTH_NUMBER = {name[:3].lower(): n for n, name in enumerate(MONTHS, start=1)}
_PHONE_KEYWORD = re.compile(
    r"\b(?:phone|mobile|cell)\b[^\d]{0,30}?(?<!\d)(\d{4})(?!\d)", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"(?<!\d)\d(?:[.\-\s]?\d){9,10}(?!\d)")
_STANDALONE_4 = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_BIRTH_KEYWORD = re.compile(r"\b(?:born|birth|dob)\b", re.IGNORECASE)


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " # " + text[span[1] :]


@dataclass(frozen=True)
class FoundDate:
    """A date-like span; ``value`` is set only when it names exactly one real date."""

    span: tuple[int, int]
    value: date | None
    problem: str | None


def _calendar_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _impossible(typed: str, year: int, month: int, day: int) -> str:
    detail = (
        f"{format_month(year, month)} doesn't have a day {day}"
        if 1 <= month <= 12
        else f"there is no month {month}"
    )
    return f"{typed} isn't a real date: {detail}. Please check it, or {_PICK_FROM_CALENDAR}."


def _read_date(m: re.Match[str], order: str) -> FoundDate:
    typed = m.group(0)
    if order == "numeric":
        first, second, year = (int(g) for g in m.groups())
        day_first = _calendar_date(year, second, first)
        month_first = _calendar_date(year, first, second)
        if day_first and month_first and day_first != month_first:
            return FoundDate(
                m.span(),
                None,
                f"Just to be sure: does {typed} mean {format_date(day_first)} or "
                f"{format_date(month_first)}? You can write the month as a word, like "
                f"“9 February 1989”, or {_PICK_FROM_CALENDAR}.",
            )
        if day_first or month_first:
            return FoundDate(m.span(), day_first or month_first, None)
        if second <= 12:
            return FoundDate(m.span(), None, _impossible(typed, year, second, first))
        return FoundDate(m.span(), None, _impossible(typed, year, first, second))
    if order == "ymd":
        year, month, day = (int(g) for g in m.groups())
    elif order == "dMy":
        day, month, year = (
            int(m.group(1)),
            _MONTH_NUMBER[m.group(2).lower()],
            int(m.group(3)),
        )
    else:  # "Mdy"
        month, day, year = (
            _MONTH_NUMBER[m.group(1).lower()],
            int(m.group(2)),
            int(m.group(3)),
        )
    value = _calendar_date(year, month, day)
    if value is None:
        return FoundDate(m.span(), None, _impossible(typed, year, month, day))
    return FoundDate(m.span(), value, None)


def find_date(text: str) -> FoundDate | None:
    """The first date-like span in ``text`` in any supported spelling, or None."""
    matches = [(m, order) for p, order in _DATE_FORMS if (m := p.search(text))]
    if not matches:
        return None
    m, order = min(matches, key=lambda pair: pair[0].start())
    return _read_date(m, order)


def date_problem(text: str) -> str | None:
    """Why a birth date in ``text`` can't be used (impossible or ambiguous), else None.

    Only for messages that look like identity details (an identifier or a birth keyword), so
    ordinary dates in other questions are left alone.
    """
    found = find_date(text)
    if found is None or found.problem is None:
        return None
    rest = _blank(text, found.span)
    if _IDENTIFIER.search(rest) or _BIRTH_KEYWORD.search(text):
        return found.problem
    return None


def parse_credentials(text: str) -> dict[str, str] | None:
    """``authenticate_customer`` arguments found in ``text``; None unless all three are present."""
    found = find_date(text)
    if found is None or found.value is None:
        return None
    birth = found.value
    rest = _blank(text, found.span)

    phone: str | None = None
    keyword = _PHONE_KEYWORD.search(rest)
    if keyword is not None:
        phone = keyword.group(1)
        rest = _blank(rest, keyword.span())

    ident = _IDENTIFIER.search(rest)
    if ident is None:
        return None
    identifier = re.sub(r"\D", "", ident.group())
    rest = _blank(rest, ident.span())

    if phone is None:
        groups = _STANDALONE_4.findall(rest)
        if not groups:
            return None
        phone = groups[-1]
    return {
        "identifier": identifier,
        "birth_date": birth.isoformat(),
        "phone_last4": phone,
    }


def has_bill_intent(text: str) -> bool:
    """Bill-related wording; a message that carries full credentials never counts."""
    if parse_credentials(text) is not None:
        return False
    return _INTENT.search(_fold(text)) is not None


# --------------------------------------------------------------------------- input access


def _is_user(item: dict) -> bool:
    return item.get("type") == "message" and item.get("role") == "user"


def _user_text(item: dict) -> str:
    content = item.get("content")
    return content if isinstance(content, str) else ""


_CALL_ID = re.compile(r"^scripted_call_(\d+)$")


def _next_call_id(items: tuple[dict, ...]) -> str:
    """One past the highest ``scripted_call_<n>`` in the input, so truncated history can't collide."""
    highest = 0
    for item in items:
        if item.get("type") not in ("function_call", "function_call_output"):
            continue
        m = _CALL_ID.match(str(item.get("call_id")))
        if m:
            highest = max(highest, int(m.group(1)))
    return f"scripted_call_{highest + 1}"


def _calls_by_id(items: tuple[dict, ...]) -> dict[str, dict]:
    return {
        i["call_id"]: i
        for i in items
        if i.get("type") == "function_call" and isinstance(i.get("call_id"), str)
    }


def _parse_output(raw: Any) -> tuple[str, Any]:
    """``(status, untrusted_data)``; anything malformed is an ``internal_error``."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("status"), str):
        return "internal_error", None
    return parsed["status"], parsed.get("untrusted_data")


def _auth_ok_outputs(items: tuple[dict, ...], calls: dict[str, dict]) -> list[dict]:
    return [
        i
        for i in items
        if i.get("type") == "function_call_output"
        and calls.get(i.get("call_id"), {}).get("name") == "authenticate_customer"
        and _parse_output(i.get("output"))[0] == "ok"
    ]


_NAME_KEEP = re.compile(r"[^\w\s-]|[\d_]", re.UNICODE)


def _safe_first_name(data: Any) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("first_name"), str):
        return None
    cleaned = _NAME_KEEP.sub("", data["first_name"])
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:40].strip()
    return cleaned or None


# --------------------------------------------------------------------------- summaries


def _parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _reference_month(value: Any) -> str | None:
    m = _MONTH.match(value) if isinstance(value, str) else None
    return format_month(int(m.group(1)), int(m.group(2))) if m else None


def _bill_line(bill: dict) -> str:
    parts: list[str] = []
    month = _reference_month(bill.get("reference_month"))
    if month:
        parts.append(month)
    amount = _parse_decimal(bill.get("amount_brl"))
    if amount is not None:
        parts.append(format_brl(amount))
    due = _parse_date(bill.get("due_date"))
    if due is not None:
        parts.append(f"due {format_date(due)}")
    status = _BILL_STATUS_EN.get(bill.get("status"))  # type: ignore[arg-type]
    if status:
        parts.append(status)
    return "- " + " — ".join(parts) if parts else "- Bill with no details available"


def _summarize_bills(data: Any, args: dict) -> str:
    if not isinstance(data, list):
        return MESSAGES["upstream_unavailable"]
    if not data:
        return _EMPTY_BILLS.get(args.get("status"), _EMPTY_BILLS[None])
    bills = [b for b in data if isinstance(b, dict)]
    bills.sort(key=lambda b: 1 if b.get("status") == "paid" else 0)  # stable
    n = len(bills)
    head = "I found 1 bill:" if n == 1 else f"I found {n} bills:"
    return "\n".join([head, *(_bill_line(b) for b in bills)])


def _summarize_bill_details(data: Any) -> str:
    if not isinstance(data, dict):
        return MESSAGES["upstream_unavailable"]
    month = _reference_month(data.get("reference_month"))
    lines = [f"Bill for {month}:" if month else "Bill details:"]
    amount = _parse_decimal(data.get("amount_brl"))
    if amount is not None:
        lines.append(f"- Amount: {format_brl(amount)}")
    due = _parse_date(data.get("due_date"))
    if due is not None:
        lines.append(f"- Due date: {format_date(due)}")
    status = _BILL_STATUS_EN.get(data.get("status"))  # type: ignore[arg-type]
    if status:
        lines.append(f"- Status: {status}")
    issued = _parse_date(data.get("issue_date"))
    if issued is not None:
        lines.append(f"- Issued: {format_date(issued)}")
    kwh = data.get("consumption_kwh")
    if isinstance(kwh, int) and not isinstance(kwh, bool):
        lines.append(f"- Consumption: {kwh} kWh")
    items = data.get("line_items")
    if isinstance(items, list) and items:
        n = len(items)
        lines.append(f"- The bill has {n} line item{'' if n == 1 else 's'}.")
    return "\n".join(lines)


def _summarize_account(data: Any) -> str:
    if not isinstance(data, dict):
        return MESSAGES["upstream_unavailable"]
    parts: list[str] = []
    installations = data.get("installations")
    if isinstance(installations, list):
        n = len(installations)
        parts.append(
            "Your account has 1 installation."
            if n == 1
            else f"Your account has {n} installations."
        )
    status = _ACCOUNT_STATUS_EN.get(data.get("account_status"))  # type: ignore[arg-type]
    if status:
        parts.append(f"Account status: {status}.")
    return " ".join(parts) or "I checked your account details."


def _summarize_output(name: str | None, args: dict, status: str, data: Any) -> str:
    if status != "ok":
        if status == "invalid_arguments":
            return (
                ASK_VERIFICATION
                if name == "authenticate_customer"
                else MESSAGES["limit_reached"]
            )
        return _ERROR_TEXT.get(status, MESSAGES["upstream_unavailable"])
    if name == "list_bills":
        return _summarize_bills(data, args)
    if name == "get_bill_details":
        return _summarize_bill_details(data)
    if name == "get_account_summary":
        return _summarize_account(data)
    return MESSAGES["limit_reached"]


# --------------------------------------------------------------------------- responses


def _text_response(text: str) -> ModelResponse:
    return ModelResponse(
        status="completed",
        text=text,
        function_calls=(),
        output_items=({"type": "message", "role": "assistant", "content": text},),
    )


def _call_response(call_id: str, name: str, args: dict) -> ModelResponse:
    arguments = json.dumps(args, ensure_ascii=False)
    return ModelResponse(
        status="completed",
        text=None,
        function_calls=(FunctionCall(call_id=call_id, name=name, arguments=arguments),),
        output_items=(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            },
        ),
    )


def _list_bills_response(call_id: str, text: str) -> ModelResponse:
    status = bill_status(text)
    return _call_response(
        call_id, "list_bills", {} if status is None else {"status": status}
    )


def estimate_request_tokens(request: ModelRequest) -> int:
    """Local estimate: the history estimate over each input item, plus the instructions."""
    turn_id = uuid.UUID(int=0)
    total = sum(
        estimate_tokens(
            Item(turn_id=turn_id, seq=i, kind="", call_id=None, model=None, payload=p)
        )
        for i, p in enumerate(request.input)
    )
    return total + len(request.instructions) // 3


class ScriptedModel:
    name = "scripted"
    live = False

    async def count_input_tokens(self, request: ModelRequest, ctx: ModelContext) -> int:
        return estimate_request_tokens(request)

    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse:
        items = request.input
        calls = _calls_by_id(items)
        call_id = _next_call_id(items)
        users = [i for i in items if _is_user(i)]
        last = items[-1] if items else None

        if last is not None and last.get("type") == "function_call_output":
            paired = calls.get(last.get("call_id"), {})
            name = paired.get("name")
            status, data = _parse_output(last.get("output"))
            # Rule 1: authentication succeeded (must stay ahead of the generic rule 2).
            if name == "authenticate_customer" and status == "ok":
                for message in reversed(users[-2:]):
                    if has_bill_intent(_user_text(message)):
                        return _list_bills_response(call_id, _user_text(message))
                first_name = _safe_first_name(data)
                greeting = f"Thank you, {first_name}! " if first_name else "Thank you! "
                return _text_response(
                    greeting
                    + "Your identity is confirmed. Can I help with your bills or "
                    "your account details?"
                )
            # Rule 2: any other tool output.
            return _text_response(
                _summarize_output(name, _decode_args(paired), status, data)
            )

        text = _user_text(users[-1]) if users else ""
        # Rule 3: credentials.
        creds = parse_credentials(text) if text else None
        if creds is not None:
            return _call_response(call_id, "authenticate_customer", creds)
        # Rule 3b: identity details with an impossible or ambiguous birth date.
        problem = date_problem(text) if text else None
        if problem is not None:
            return _text_response(problem)
        # Rule 4: bill question after verification.
        if text and has_bill_intent(text) and _auth_ok_outputs(items, calls):
            return _list_bills_response(call_id, text)
        # Rule 5: bill intent without visible verification.
        if text and has_bill_intent(text):
            return _text_response(ASK_VERIFICATION)
        # Rule 6: greeting.
        return _text_response(GREETING)


def _decode_args(call: dict) -> dict:
    try:
        parsed = json.loads(call.get("arguments", "{}"))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
