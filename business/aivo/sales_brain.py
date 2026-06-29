"""
AIVO Sales Brain

Responsavel por identificar a intencao do cliente
e definir o objetivo da resposta antes de chamar a OpenAI.
"""

from business.aivo.intents import Intent
from business.aivo.lead_state import LeadState


class SalesBrain:

    def evaluate(self, message: str):

        raw_text = str(message or "").strip()
        normalized = raw_text.lower().strip(" ?!.,;:\\n\\t")

        if normalized in {"oi", "ola", "olá", "bom dia", "boa tarde", "boa noite"}:
            return {
                "intent": Intent.GREETING,
                "lead_state": LeadState.NEW,
                "goal": "Responder uma saudacao curta e puxar uma pergunta leve de qualificacao.",
            }

        if normalized in {"presencial", "online", "reuniao presencial", "reunião presencial", "reuniao online", "reunião online"}:
            return {
                "intent": Intent.HOW_IT_WORKS,
                "lead_state": LeadState.QUALIFIED,
                "goal": "Responder dando continuidade ao uso informado pelo cliente e conduzir para demonstracao, preco ou compra.",
            }


        text = message.lower().strip()
        compact = " ".join(text.split())

        intent = Intent.UNKNOWN
        lead_state = LeadState.NEW
        goal = "responder"

        # Compra
        if any(x in text for x in [
            "comprar",
            "como compro",
            "como eu compro",
            "quero comprar",
            "pedido",
            "finalizar",
            "pagamento",
            "cartao",
            "cartão",
            "parcelas",
            "parcela",
            "à vista",
            "a vista",
        ]):
            intent = Intent.BUY
            lead_state = LeadState.BUYING
            goal = "fechar_venda"

        # Preco
        elif any(x in text for x in [
            "preco",
            "valor",
            "quanto custa",
            "plano",
            "planos",
            "quanto fica",
            "forma de pagamento",
            "condicao de pagamento",
            "condições de pagamento",
        ]):
            intent = Intent.PRICE
            lead_state = LeadState.BUYING
            goal = "apresentar_planos"

        elif any(x in compact for x in [
            "2x",
            "12x",
            "sem juros",
            "com acrescimos",
            "com acréscimos",
        ]):
            intent = Intent.PRICE
            lead_state = LeadState.BUYING
            goal = "apresentar_condicao_pagamento"

        # Video
        elif any(x in text for x in [
            "video",
            "demonstracao",
            "demo",
            "mostrar",
        ]):
            intent = Intent.VIDEO
            lead_state = LeadState.INTERESTED
            goal = "mostrar_demo"

        # Funcionamento
        elif any(x in text for x in [
            "como funciona",
            "funciona",
            "explica",
        ]):
            intent = Intent.HOW_IT_WORKS
            lead_state = LeadState.INTERESTED
            goal = "explicar_funcionamento"

        return {
            "intent": intent,
            "lead_state": lead_state,
            "goal": goal,
        }
