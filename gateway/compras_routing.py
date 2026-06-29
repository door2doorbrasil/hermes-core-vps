from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Sequence
import unicodedata

from agent.prompt_builder import VISION_PRODUCT_SOURCING_PROMPT

logger = logging.getLogger(__name__)

COMPRAS_MODEL_NAME = "hermes-compras"

_STRONG_SOURCE_TERMS = (
    "supplier sourcing",
    "product sourcing",
    "rfq",
    "rfq request",
    "request for quotation",
    "quotation",
    "quote",
    "supplier",
    "suppliers",
    "manufacturer",
    "manufacturers",
    "fabricante",
    "fabricantes",
    "fornecedor",
    "fornecedores",
    "sourcing",
    "procurement",
    "purchasing",
    "buying",
    "cotacao",
    "cotar",
    "cotação",
)

_WEAK_PRODUCT_TERMS = (
    "product",
    "produto",
    "item",
    "imagem",
    "image",
    "foto",
    "photo",
    "picture",
    "catalog",
    "catalogo",
    "catálogo",
    "datasheet",
    "specification",
    "technical sheet",
)


@dataclass(frozen=True, slots=True)
class ComprasRoutingDecision:
    requested_model: str
    selected_model: str
    intent: str
    modality: str
    reason: str
    matched_terms: tuple[str, ...] = ()
    vision_product_sourcing: bool = False
    ephemeral_prompt_suffix: str = ""

    @property
    def route_to_compras(self) -> bool:
        return self.selected_model.lower().startswith(COMPRAS_MODEL_NAME)



def _normalize_text(value: Any) -> str:
    text = ""
    if value is None:
        return text
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        if "text" in value:
            text = _normalize_text(value.get("text"))
        elif "content" in value:
            text = _normalize_text(value.get("content"))
        else:
            text = " ".join(
                _normalize_text(v)
                for v in value.values()
                if isinstance(v, (str, list, dict))
            )
    elif isinstance(value, list):
        text = " ".join(_normalize_text(item) for item in value)
    else:
        text = str(value)
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()



def _extract_content_text(message: Any) -> str:
    if isinstance(message, dict):
        if "content" in message:
            return _normalize_text(message.get("content"))
        if "text" in message:
            return _normalize_text(message.get("text"))
    return _normalize_text(message)



def _conversation_text(conversation_history: Sequence[Any] | None) -> str:
    if not conversation_history:
        return ""
    return "\n".join(
        part for part in (_extract_content_text(entry).strip() for entry in conversation_history) if part
    )



def _match_terms(text: str, terms: Sequence[str]) -> tuple[str, ...]:
    matches = []
    for term in terms:
        if term in text:
            matches.append(term)
    return tuple(matches)



def classify_compras_intent(
    user_message: Any,
    *,
    conversation_history: Sequence[Any] | None = None,
    system_prompt: str | None = None,
) -> tuple[str, tuple[str, ...], str]:
    """Classify a turn as compras/sourcing or general."""
    parts = [
        _normalize_text(system_prompt or "").strip(),
        _conversation_text(conversation_history).strip(),
        _extract_content_text(user_message).strip(),
    ]
    searchable = "\n".join(part for part in parts if part)
    strong_matches = _match_terms(searchable, _STRONG_SOURCE_TERMS)
    if strong_matches:
        return "supplier_sourcing", strong_matches, searchable

    weak_matches = _match_terms(searchable, _WEAK_PRODUCT_TERMS)
    if weak_matches:
        return "supplier_sourcing", weak_matches, searchable

    return "general", (), searchable



def route_compras_turn(
    current_model: str,
    user_message: Any,
    *,
    conversation_history: Sequence[Any] | None = None,
    system_prompt: str | None = None,
    has_image: bool = False,
    has_attachment: bool = False,
) -> ComprasRoutingDecision:
    """Select the model and multimodal prompt for a single turn."""
    requested_model = (current_model or "").strip() or COMPRAS_MODEL_NAME
    normalized_requested = requested_model.lower()
    modality = "multimodal" if (has_image or has_attachment) else "text"

    intent, matched_terms, _ = classify_compras_intent(
        user_message,
        conversation_history=conversation_history,
        system_prompt=system_prompt,
    )

    is_compras_model = normalized_requested == COMPRAS_MODEL_NAME or normalized_requested.startswith(f"{COMPRAS_MODEL_NAME}-")
    selected_model = requested_model
    reason = "no compras intent detected"
    vision_product_sourcing = False

    if is_compras_model:
        selected_model = requested_model
        reason = "current model already hermes-compras"
    elif intent == "supplier_sourcing":
        selected_model = COMPRAS_MODEL_NAME
        if has_image or has_attachment:
            vision_product_sourcing = True
            reason = "multimodal sourcing intent detected"
        else:
            reason = "sourcing intent detected"
    elif has_image and intent == "general":
        reason = "image present but no sourcing intent detected"

    ephemeral_prompt_suffix = VISION_PRODUCT_SOURCING_PROMPT if vision_product_sourcing else ""
    decision = ComprasRoutingDecision(
        requested_model=requested_model,
        selected_model=selected_model,
        intent=intent,
        modality=modality,
        reason=reason,
        matched_terms=matched_terms,
        vision_product_sourcing=vision_product_sourcing,
        ephemeral_prompt_suffix=ephemeral_prompt_suffix,
    )

    log_level = logging.INFO if decision.route_to_compras or decision.vision_product_sourcing or is_compras_model else logging.DEBUG
    logger.log(
        log_level,
        "[compras-routing] modality=%s intent=%s selected_model=%s reason=%s matched_terms=%s vision=%s",
        decision.modality,
        decision.intent,
        decision.selected_model,
        decision.reason,
        ",".join(decision.matched_terms) if decision.matched_terms else "",
        decision.vision_product_sourcing,
    )
    return decision



def apply_compras_prompt_suffix(ephemeral_prompt: str | None, decision: ComprasRoutingDecision) -> str | None:
    """Append the sourcing vision prompt when the decision requires it."""
    pieces = []
    base = (ephemeral_prompt or "").strip()
    if base:
        pieces.append(base)
    suffix = (decision.ephemeral_prompt_suffix or "").strip()
    if suffix:
        pieces.append(suffix)
    if not pieces:
        return None
    return "\n\n".join(pieces)
