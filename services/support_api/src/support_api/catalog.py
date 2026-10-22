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


# Static browser copy. Export the fallback snapshot with scripts/export-ui-catalog.py.
UI_MESSAGES: dict[str, str] = {
    "brand": "Vértice Energia",
    "fictional": "Empresa fictícia · demonstração",
    "eyebrow": "ATENDIMENTO DIGITAL",
    "title": "Sua energia, nossa conversa.",
    "intro": "Consulte sua conta e suas faturas com o assistente virtual.",
    "chat_title": "Como podemos ajudar?",
    "chat_subtitle": "Assistente virtual da Vértice Energia",
    "welcome": "Olá! Posso ajudar com suas faturas em aberto, em atraso ou pagas. O que você gostaria de consultar?",
    "assistant": "Vértice",
    "you": "Você",
    "message_label": "Sua mensagem",
    "placeholder": "Escreva sua mensagem…",
    "send": "Enviar mensagem",
    "sending": "Consultando…",
    "message_hint": "Até 2.000 caracteres. Use apenas os dados sintéticos da demonstração.",
    "too_long": "Sua mensagem deve ter até 2.000 caracteres.",
    "retry": "Tentar novamente",
    "network_error": "Não foi possível conectar. Verifique sua conexão e tente novamente.",
    "loading": "Preparando seu atendimento…",
    "demo_heading": "Dados de demonstração",
    "synthetic": "dados sintéticos",
    "demo_intro": "Escolha um perfil abaixo para testar a consulta. Quando solicitado, informe o CPF, a data de nascimento e o final do telefone.",
    "demo_unavailable": "Os dados de demonstração estão indisponíveis no momento.",
    "cpf": "CPF",
    "account": "Conta",
    "birth_date": "Nascimento",
    "phone_last4": "Final do telefone",
    "demo_notice": "Todos os nomes, documentos e dados de consumo são fictícios.",
    "transcript": "Histórico da conversa",
    "unavailable": "Não foi possível concluir sua solicitação no momento. Tente novamente em alguns minutos.",
    "footer": "Vértice Energia · Projeto de demonstração com dados sintéticos",
}
