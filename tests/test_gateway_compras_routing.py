from __future__ import annotations

from agent.prompt_builder import build_model_persona_prompt
from gateway.compras_routing import (
    COMPRAS_MODEL_NAME,
    VISION_PRODUCT_SOURCING_PROMPT,
    apply_compras_prompt_suffix,
    route_compras_turn,
)


def test_textual_sourcing_routes_to_hermes_compras() -> None:
    decision = route_compras_turn(
        "gpt-4.1",
        "Preciso de fornecedores e RFQ para este produto.",
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.intent == "supplier_sourcing"
    assert decision.route_to_compras is True
    assert decision.vision_product_sourcing is False
    assert decision.modality == "text"
    assert decision.ephemeral_prompt_suffix == ""


def test_manufacturer_intent_routes_to_hermes_compras() -> None:
    decision = route_compras_turn(
        "openai/gpt-4.1",
        "Qual fabricante produz este item?",
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.intent == "supplier_sourcing"
    assert "fabricante" in decision.matched_terms or "fabricantes" in decision.matched_terms


def test_rfq_intent_routes_to_hermes_compras() -> None:
    decision = route_compras_turn(
        "claude-3.5",
        "Monte um RFQ e uma cotação para esse fornecedor.",
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.intent == "supplier_sourcing"
    assert "rfq" in decision.matched_terms or "quotation" in decision.matched_terms


def test_multimodal_sourcing_triggers_vision_product_sourcing() -> None:
    decision = route_compras_turn(
        "gpt-4.1",
        "Cotar este produto da imagem.",
        has_image=True,
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.intent == "supplier_sourcing"
    assert decision.vision_product_sourcing is True
    assert decision.ephemeral_prompt_suffix == VISION_PRODUCT_SOURCING_PROMPT
    assert apply_compras_prompt_suffix("base prompt", decision) == "base prompt\n\n" + VISION_PRODUCT_SOURCING_PROMPT.rstrip()


def test_vision_product_sourcing_off_without_image() -> None:
    decision = route_compras_turn(
        "gpt-4.1",
        "Preciso de fornecedores para este produto.",
        has_image=False,
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.vision_product_sourcing is False
    assert decision.ephemeral_prompt_suffix == ""


def test_out_of_scope_keeps_compras_model_controlled() -> None:
    decision = route_compras_turn(
        COMPRAS_MODEL_NAME,
        "Explique teoria dos números complexos.",
    )
    assert decision.selected_model == COMPRAS_MODEL_NAME
    assert decision.intent == "general"
    assert decision.route_to_compras is True
    assert decision.vision_product_sourcing is False
    assert decision.reason == "current model already hermes-compras"


def test_out_of_scope_persona_prompt_stays_controlled() -> None:
    prompt = build_model_persona_prompt(COMPRAS_MODEL_NAME)
    assert "Este pedido está fora do escopo do Hermes Compras" in prompt
    assert "generic web browsing is not allowed in the web ui" in prompt.lower()
