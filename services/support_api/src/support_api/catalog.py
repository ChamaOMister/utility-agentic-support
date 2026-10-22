"""Customer-facing PT-BR messages. All user-visible text that is not model output lives here."""

MESSAGES: dict[str, str] = {
    "auth_failed": "Não foi possível confirmar seus dados. Verifique as informações e tente novamente mais tarde.",
    "verification_expired": "Por segurança, sua verificação expirou. Para continuar, confirme seus dados novamente.",
    "upstream_unavailable": "Não foi possível concluir sua solicitação no momento. Tente novamente em alguns minutos.",
    "limit_reached": "Não consegui concluir sua solicitação agora. Tente novamente em instantes.",
    "live_blocked": "As respostas automáticas estão indisponíveis nesta conversa no momento. Tente novamente mais tarde.",
    "turn_in_progress": "Ainda estou processando sua mensagem anterior. Aguarde um momento.",
    "turn_interrupted": "Não foi possível concluir sua mensagem. Envie novamente para tentar outra vez.",
    "offline_badge": "Modo offline — respostas simuladas",
}


def message(key: str) -> str:
    return MESSAGES[key]
