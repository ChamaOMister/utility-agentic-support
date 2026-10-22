"""Deterministic, rule-based PT-BR model for the offline demo (spec §5.6).

It reads only ``request.input`` and never sends anything anywhere, token counts included. Its tool
calls go through the real executor and billing client; its text is built from fixed PT-BR templates
plus structured tool data. Tool output is ``untrusted_data``: this module interpolates only fields it
has parsed into numbers, dates or members of fixed vocabularies, plus a sanitised ``first_name``.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from support_api.agent.format_ptbr import format_brl, format_date
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
    "Para consultar dados da sua conta, preciso confirmar sua identidade. Informe seu CPF ou "
    "número da conta, sua data de nascimento (dd/mm/aaaa) e os 4 últimos dígitos do telefone "
    "cadastrado."
)
GREETING = (
    "Olá! Sou o assistente virtual da Vértice Energia (empresa fictícia, projeto de demonstração). "
    "Posso ajudar você a consultar suas faturas em aberto, em atraso ou pagas. Como posso ajudar?"
)
BILL_NOT_FOUND = "Não encontrei essa fatura. Confira o identificador e tente novamente."
_EMPTY_BILLS = {
    "open": "Não encontrei faturas em aberto.",
    "overdue": "Não encontrei faturas em atraso.",
    "paid": "Não encontrei faturas pagas.",
    None: "Não encontrei faturas na sua conta.",
}
_BILL_STATUS_PT = {"open": "em aberto", "overdue": "em atraso", "paid": "paga"}
_ACCOUNT_STATUS_PT = {"active": "ativa"}
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


_INTENT = re.compile(r"fatura|conta|boleto|pagar|debito|vencimento")
_STATUS_PATTERNS: dict[str, re.Pattern[str]] = {
    "open": re.compile(r"\babert[oa]s?\b"),
    "overdue": re.compile(r"\batras(o|ad[oa]s?)\b|\bvencid[oa]s?\b"),
    "paid": re.compile(r"\bpag[oa]s?\b|\bquitad[oa]s?\b"),
}


def bill_status(text: str) -> str | None:
    """The requested bill status, only when exactly one category matches."""
    folded = _fold(text)
    found = [s for s, p in _STATUS_PATTERNS.items() if p.search(folded)]
    return found[0] if len(found) == 1 else None


_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
_PHONE_KEYWORD = re.compile(
    r"\b(?:telefone|celular)\b[^\d]{0,30}?(?<!\d)(\d{4})(?!\d)", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"(?<!\d)\d(?:[.\-\s]?\d){9,10}(?!\d)")
_STANDALONE_4 = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " # " + text[span[1] :]


def parse_credentials(text: str) -> dict[str, str] | None:
    """``authenticate_customer`` arguments found in ``text``; None unless all three are present."""
    date_match = _DATE.search(text)
    if date_match is None:
        return None
    day, month, year = (int(g) for g in date_match.groups())
    try:
        birth = date(year, month, day)
    except ValueError:
        return None
    rest = _blank(text, date_match.span())

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
    return f"{m.group(2)}/{m.group(1)}" if m else None


def _bill_line(bill: dict) -> str:
    parts: list[str] = []
    month = _reference_month(bill.get("reference_month"))
    if month:
        parts.append(f"Referência {month}")
    amount = _parse_decimal(bill.get("amount_brl"))
    if amount is not None:
        parts.append(format_brl(amount))
    due = _parse_date(bill.get("due_date"))
    if due is not None:
        parts.append(f"vencimento {format_date(due)}")
    status = _BILL_STATUS_PT.get(bill.get("status"))  # type: ignore[arg-type]
    if status:
        parts.append(status)
    return "- " + " — ".join(parts) if parts else "- Fatura sem detalhes disponíveis"


def _summarize_bills(data: Any, args: dict) -> str:
    if not isinstance(data, list):
        return MESSAGES["upstream_unavailable"]
    if not data:
        return _EMPTY_BILLS.get(args.get("status"), _EMPTY_BILLS[None])
    bills = [b for b in data if isinstance(b, dict)]
    bills.sort(key=lambda b: 1 if b.get("status") == "paid" else 0)  # stable
    n = len(bills)
    head = "Encontrei 1 fatura:" if n == 1 else f"Encontrei {n} faturas:"
    return "\n".join([head, *(_bill_line(b) for b in bills)])


def _summarize_bill_details(data: Any) -> str:
    if not isinstance(data, dict):
        return MESSAGES["upstream_unavailable"]
    month = _reference_month(data.get("reference_month"))
    lines = [f"Fatura de {month}:" if month else "Detalhes da fatura:"]
    amount = _parse_decimal(data.get("amount_brl"))
    if amount is not None:
        lines.append(f"- Valor: {format_brl(amount)}")
    due = _parse_date(data.get("due_date"))
    if due is not None:
        lines.append(f"- Vencimento: {format_date(due)}")
    status = _BILL_STATUS_PT.get(data.get("status"))  # type: ignore[arg-type]
    if status:
        lines.append(f"- Situação: {status}")
    issued = _parse_date(data.get("issue_date"))
    if issued is not None:
        lines.append(f"- Emissão: {format_date(issued)}")
    kwh = data.get("consumption_kwh")
    if isinstance(kwh, int) and not isinstance(kwh, bool):
        lines.append(f"- Consumo: {kwh} kWh")
    items = data.get("line_items")
    if isinstance(items, list) and items:
        lines.append(f"- A fatura tem {len(items)} itens de cobrança.")
    return "\n".join(lines)


def _summarize_account(data: Any) -> str:
    if not isinstance(data, dict):
        return MESSAGES["upstream_unavailable"]
    parts: list[str] = []
    installations = data.get("installations")
    if isinstance(installations, list):
        n = len(installations)
        parts.append(
            "Sua conta tem 1 instalação."
            if n == 1
            else f"Sua conta tem {n} instalações."
        )
    status = _ACCOUNT_STATUS_PT.get(data.get("account_status"))  # type: ignore[arg-type]
    if status:
        parts.append(f"A situação da conta é: {status}.")
    return " ".join(parts) or "Consultei os dados da sua conta."


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

    async def count_input_tokens(self, request: ModelRequest, ctx: ModelContext) -> int:
        return estimate_request_tokens(request)

    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse:
        items = request.input
        calls = _calls_by_id(items)
        call_id = f"scripted_call_{sum(1 for i in items if i.get('type') == 'function_call') + 1}"
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
                greeting = f"Obrigado, {first_name}! " if first_name else "Obrigado! "
                return _text_response(
                    greeting
                    + "Sua identidade foi confirmada. Posso ajudar com suas faturas ou "
                    "com os dados da sua conta?"
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
