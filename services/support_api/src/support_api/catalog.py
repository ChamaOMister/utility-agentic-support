"""Customer-facing English messages. All user-visible text that is not model output lives here."""

MESSAGES: dict[str, str] = {
    "auth_failed": "We couldn't confirm your details. Please check the information and try again later.",
    "verification_expired": "For your security, your verification has expired. To continue, please confirm your details again.",
    "upstream_unavailable": "We couldn't complete your request right now. Please try again in a few minutes.",
    "limit_reached": "I couldn't complete your request right now. Please try again in a moment.",
    "live_blocked": "Automatic replies are unavailable in this conversation right now. Please try again later.",
    "turn_in_progress": "I'm still working on your previous message. Please wait a moment.",
    "turn_interrupted": "We couldn't finish your message. Send it again to retry.",
    "offline_badge": "Offline mode — simulated replies",
}


def message(key: str) -> str:
    return MESSAGES[key]


# Static browser copy. Export the fallback snapshot with scripts/export-ui-catalog.py.
UI_MESSAGES: dict[str, str] = {
    "brand": "Vértice Energia",
    "fictional": "Fictional company · demo",
    "eyebrow": "DIGITAL SUPPORT",
    "title": "Your energy, our conversation.",
    "intro": "Check your account and your bills with our virtual assistant.",
    "chat_title": "How can we help?",
    "chat_subtitle": "Vértice Energia virtual assistant",
    "welcome": "Hello! I can help with your open, overdue or paid bills. What would you like to check?",
    "assistant": "Vértice",
    "you": "You",
    "message_label": "Your message",
    "placeholder": "Type your message…",
    "send": "Send message",
    "sending": "Looking that up…",
    "message_hint": "Up to 2,000 characters. Use only the synthetic demo data.",
    "too_long": "Your message must be 2,000 characters or fewer.",
    "retry": "Try again",
    "network_error": "We couldn't connect. Check your connection and try again.",
    "loading": "Getting your support session ready…",
    "demo_heading": "Demo data",
    "synthetic": "synthetic data",
    "demo_intro": "Pick a profile below to try a lookup. When asked, use “Verify my identity” or type the CPF, date of birth and last 4 phone digits.",
    "demo_unavailable": "The demo data is unavailable right now.",
    "cpf": "CPF",
    "account": "Account",
    "birth_date": "Date of birth",
    "phone_last4": "Phone ends in",
    "demo_notice": "All names, documents and usage data are fictional.",
    "transcript": "Conversation history",
    "unavailable": "We couldn't complete your request right now. Please try again in a few minutes.",
    "footer": "Vértice Energia · Demo project with synthetic data",
    "verify_open": "Verify my identity",
    "verify_title": "Verify my identity",
    "verify_identifier": "CPF or account number",
    "verify_identifier_error": "Enter an 11-digit CPF or a 10-digit account number.",
    "verify_birth_date": "Date of birth",
    "verify_birth_date_error": "Pick your date of birth from the calendar.",
    "verify_phone": "Last 4 digits of your phone",
    "verify_phone_error": "Enter exactly 4 digits.",
    "verify_submit": "Verify",
    "verify_cancel": "Cancel",
    "verify_message": "My CPF or account number is {identifier}, my date of birth is {birth_date} and the last 4 digits of my phone are {phone_last4}.",
}
