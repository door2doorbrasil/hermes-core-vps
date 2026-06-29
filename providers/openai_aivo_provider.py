from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from agent.secret_scope import get_secret
from hermes_cli.config import cfg_get, load_config
from aivo_learning import ensure_aivo_learning_store
from business.aivo.product_analysis import format_product_analysis_block
from providers.provider_result import ProviderResult
from business.aivo.sales_brain import SalesBrain
from business.aivo.prompts import build_intent_prompt

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_TEMPERATURE = 0.4
DEFAULT_MAX_OUTPUT_TOKENS = 260

FORBIDDEN_INTERNAL_TERMS = (
    "chatgpt",
    "openai",
    "hermes",
    "skill",
    "arquivo",
    "busca",
    "sessao",
    "session",
    "log",
    "prompt",
)

AIVO_SYSTEM_PROMPT = """
Voce e o atendimento comercial de insumos industriais no WhatsApp.

Responda somente a mensagem final para o cliente.

O agente vende bicarbonato de sodio, bicarbonato de amonio, bicarbonato de potassio,
sagu, fecula de batata, fecula de mandioca, polvilho doce e polvilho azedo.

Outros insumos e materias-primas similares podem ser cadastrados tambem.

Regras:
- Nao apresente como chatbot.
- Nao apresente como CRM.
- Nao apresente como automacao de WhatsApp.
- Nao fale de lead, leads, captacao ou conversao.
- Nao mencione ChatGPT, OpenAI, Hermes, skill, arquivo, busca, sessao, log ou prompt.
- Nunca compartilhe senhas, chaves, tokens, arquivos sensiveis ou dados privados.
- Se o usuario pedir qualquer informacao sensivel, recuse de forma curta e segura.
- Nao comece com "Claro".
- Use frases curtas.
- Escreva em portugues do Brasil natural.
- Quando o assunto for preco, lote, embalagem, compra ou pagamento, mantenha a condicao comercial aprovada.
- Quando fizer sentido, termine com uma pergunta curta e objetiva.

Primeiro contato:
Oi! Como posso ajudar com o insumo que voce quer cotar?

Voce quer informar o produto, a aplicacao ou a especificacao tecnica?
""".strip()


class AivoOpenAIConfigurationError(RuntimeError):
    """Raised when the OpenAI-backed AIVO provider cannot be configured."""


class AivoOpenAIProviderError(RuntimeError):
    """Raised when the OpenAI-backed AIVO provider receives a bad response."""


@dataclass(slots=True)
class OpenAivoProvider:
    brain = SalesBrain()

    api_key: str
    model: str
    temperature: float
    base_url: str = ""
    timeout: float = 45.0
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    system_prompt: str = AIVO_SYSTEM_PROMPT

    @classmethod
    def from_runtime(cls) -> "OpenAivoProvider":
        cfg = load_config()
        model = _first_nonempty(
            cfg_get(cfg, "model", "default"),
            cfg_get(cfg, "aivo", "model"),
            os.getenv("AIVO_OPENAI_MODEL"),
            os.getenv("OPENAI_MODEL"),
            os.getenv("MODEL"),
            DEFAULT_OPENAI_MODEL,
        )
        temperature_raw = _first_nonempty(
            cfg_get(cfg, "model", "temperature"),
            cfg_get(cfg, "aivo", "temperature"),
            os.getenv("AIVO_OPENAI_TEMPERATURE"),
            os.getenv("OPENAI_TEMPERATURE"),
            os.getenv("TEMPERATURE"),
            DEFAULT_OPENAI_TEMPERATURE,
        )
        try:
            temperature = float(temperature_raw)
        except Exception as exc:
            raise AivoOpenAIConfigurationError(
                f"Invalid AIVO temperature value: {temperature_raw!r}"
            ) from exc

        api_key = _resolve_api_key(cfg)
        if not api_key:
            raise AivoOpenAIConfigurationError(
                "OpenAI API key not found in Hermes config or environment. "
                "Set model.api_key in config.yaml or OPENAI_API_KEY in .env/environment, "
                "then restart the gateway."
            )

        base_url = _first_nonempty(
            cfg_get(cfg, "model", "base_url"),
            cfg_get(cfg, "aivo", "base_url"),
            os.getenv("OPENAI_BASE_URL"),
            "",
        )
        timeout_raw = _first_nonempty(
            cfg_get(cfg, "model", "timeout"),
            cfg_get(cfg, "aivo", "timeout"),
            os.getenv("AIVO_OPENAI_TIMEOUT"),
            os.getenv("OPENAI_TIMEOUT"),
            45.0,
        )
        try:
            timeout = float(timeout_raw)
        except Exception:
            timeout = 45.0

        max_output_tokens = _coerce_int(
            _first_nonempty(
                cfg_get(cfg, "model", "max_output_tokens"),
                cfg_get(cfg, "aivo", "max_output_tokens"),
                DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            default=DEFAULT_MAX_OUTPUT_TOKENS,
        )

        return cls(
            api_key=api_key,
            model=str(model).strip(),
            temperature=temperature,
            base_url=str(base_url).strip(),
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )

    def answer(self, user_message: str, learning_context: Any = None, conversation_mode: str = "sales") -> ProviderResult:
        prompt_context = _normalize_learning_context(learning_context, conversation_mode)
        decision = self.brain.evaluate(user_message)
        instructions = build_intent_prompt(decision["intent"])
        instructions += "\n\nObjetivo da resposta:\n" + str(decision["goal"])
        context_block = _compose_learning_context_block(prompt_context, conversation_mode)
        if context_block:
            instructions = instructions + "\n\n" + context_block
        if str(conversation_mode or "").lower() == "remarketing":
            instructions = instructions + (
                "\n\nContexto adicional: esta é uma conversa de remarketing. Seja natural, acolhedor, direto e um pouco mais vendedor do que institucional. "
                "Mostre o benefício do AIVO Note, a oportunidade de comprar agora e mencione claramente que há 10% de desconto. "
                "Se precisar direcionar para a compra, use este link: https://www.aivonote.com.br/discount/AIVONOTE10?redirect=%2Fproducts%2Fgravador-digital-com-inteligencia-artificial-6996073195503"
            )
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": _compose_prompt(user_message),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        response_data = self._request(payload)
        answer = clean_final_answer(_extract_text(response_data))
        answer = enforce_payment_terms(answer, decision)
        answer = enforce_plan_and_checkout(answer, decision)
        if not answer:
            answer = _default_fallback_reply()
        return ProviderResult(
            final_response=answer,
            api_calls=1,
            provider="openai",
            route="aivo_openai",
            metadata={
                "model": self.model,
                "conversation_mode": str(conversation_mode or "sales"),
                "intent": str(decision["intent"]),
                "lead_state": str(decision["lead_state"]),
                "goal": str(decision["goal"]),
            },
        )

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _resolve_responses_url(self.base_url)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise AivoOpenAIProviderError(
                f"OpenAI HTTP {exc.code}: {body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AivoOpenAIProviderError(f"OpenAI request failed: {exc}") from exc
        except Exception as exc:
            raise AivoOpenAIProviderError(f"OpenAI request failed: {exc}") from exc


def clean_final_answer(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        low = s.lower()

        if low.startswith(("read_file", "search_files", "session_search", "skill_view", "tool call", "thinking")):
            continue
        if any(term in low for term in FORBIDDEN_INTERNAL_TERMS):
            continue
        if s.startswith(("📖", "🔎", "🔍", "📚")):
            continue

        lines.append(line)

    out = "\n".join(lines).strip()
    for bad in ("Claro!", "Claro —", "Claro,", "Claro"):
        if out.startswith(bad):
            out = out[len(bad):].strip(" -—,\n")
    return out.strip()


def enforce_payment_terms(text: str, decision: dict[str, Any]) -> str:
    answer = str(text or "").strip()
    if not answer:
        return answer

    intent = str(decision.get("intent") or "").strip()
    goal = str(decision.get("goal") or "").strip()
    if intent not in {"Intent.PRICE", "Intent.BUY", "PRICE", "BUY"} and goal not in {"apresentar_planos", "apresentar_condicao_pagamento", "fechar_venda"}:
        return answer

    canonical_terms = "2x sem juros no cartao de credito ou ate 12x com acrescimos no cartao de credito"
    lowered = answer.lower()
    has_2x = "2x sem juros" in lowered
    has_12x = "12x" in lowered and ("acresc" in lowered or "acrésc" in lowered)
    if has_2x and has_12x:
        return answer

    suffix = f"Pagamento no cartao: {canonical_terms}."
    if answer.endswith((".", "!", "?")):
        return f"{answer} {suffix}"
    return f"{answer}. {suffix}"


def enforce_plan_and_checkout(text: str, decision: dict[str, Any]) -> str:
    answer = str(text or "").strip()
    if not answer:
        return answer

    intent = str(decision.get("intent") or "").strip()
    goal = str(decision.get("goal") or "").strip()
    if intent not in {"Intent.PRICE", "Intent.BUY", "PRICE", "BUY"} and goal not in {"apresentar_planos", "apresentar_condicao_pagamento", "fechar_venda"}:
        return answer

    lowered = answer.lower()
    has_starter = bool(re.search(r"\bstarter\b", lowered))
    has_pro = bool(re.search(r"\bpro\b", lowered))
    has_max = bool(re.search(r"\bmax\b", lowered))
    has_checkout = "link oficial" in lowered or "aivonote.com.br" in lowered or "checkout" in lowered

    lines = [answer.rstrip()]
    if not (has_starter and has_pro and has_max):
        lines.append(
            "Starter: 600 minutos/mês, 81 idiomas e modelos profissionais limitados.\n"
            "Pro: R$ 149,90/ano, transcrição ilimitada, 81 idiomas e acesso total aos modelos profissionais.\n"
            "Max: R$ 499,90/ano, transcrição ilimitada, 118 idiomas e acesso total aos modelos profissionais."
        )
    if not has_checkout:
        lines.append("Você pode comprar com segurança pelo link oficial: https://aivonote.com.br")

    return "\n\n".join(part.strip() for part in lines if part.strip())


def _resolve_api_key(cfg: dict[str, Any] | None) -> str:
    candidates: Iterable[str] = (
        str(cfg_get(cfg, "model", "api_key") or "").strip(),
        str(cfg_get(cfg, "aivo", "api_key") or "").strip(),
        _secret_value("OPENAI_API_KEY"),
        _secret_value("OPENAI_KEY"),
        _secret_value("OPENAI_TOKEN"),
    )
    for value in candidates:
        if value:
            return value
    return ""


def _secret_value(name: str) -> str:
    try:
        return (get_secret(name, "") or "").strip()
    except Exception:
        return ""


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        return value
    return ""


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _resolve_responses_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if not base_url:
        return "https://api.openai.com/v1/responses"
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/responses"):
        return trimmed
    return trimmed + "/responses"


def _format_recent_history(recent_history: Any) -> str:
    if not recent_history:
        return "Sem historico relevante."

    items = recent_history if isinstance(recent_history, list) else [recent_history]
    lines: list[str] = []
    for item in items[-8:]:
        role, text = _history_item(item)
        if role not in {"user", "assistant"} or not text:
            continue
        role_label = "Cliente" if role == "user" else "Atendente"
        clean = _clean_context_text(text)
        clean = _shrink_text(clean, 500)
        if clean:
            lines.append(f"{role_label}: {clean}")

    if not lines:
        return "Sem historico relevante."
    joined = "\n".join(lines)
    return _shrink_text(joined, 1800)


def _history_item(item: Any) -> tuple[str, str]:
    role = ""
    text = ""
    if isinstance(item, dict):
        role = str(item.get("role") or item.get("speaker") or item.get("author") or "").strip().lower()
        for key in ("content", "text", "message", "body"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
            if isinstance(value, list):
                parts = []
                for entry in value:
                    if isinstance(entry, dict):
                        maybe = entry.get("text") or entry.get("content")
                        if isinstance(maybe, str) and maybe.strip():
                            parts.append(maybe.strip())
                if parts:
                    text = "\n".join(parts)
                    break
        if not text and "output_text" in item and isinstance(item.get("output_text"), str):
            text = item["output_text"].strip()
    else:
        text = str(item).strip()
    return role, text


def _shrink_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clean_context_text(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith(("read_file", "search_files", "session_search", "skill_view", "tool call", "thinking")):
            continue
        if any(term in low for term in FORBIDDEN_INTERNAL_TERMS):
            continue
        if s.startswith(("📖", "🔎", "🔍", "📚")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _compose_prompt(user_message: str) -> str:
    return (
        "Mensagem atual do cliente:\n"
        f"{user_message}\n"
        "Responda somente a mensagem final para WhatsApp."
    )


def _normalize_learning_context(learning_context: Any, conversation_mode: str) -> dict[str, Any]:
    if isinstance(learning_context, dict):
        ctx = dict(learning_context)
    else:
        ctx = {}
    if ctx.get("approved_playbook") and ctx.get("approved_faq") and ctx.get("contact_profile") is not None:
        return ctx
    try:
        store = ensure_aivo_learning_store()
        fallback = store.build_prompt_context(
            contact_id=str(ctx.get("contact_id") or "unknown"),
            sender_id=str(ctx.get("sender_id") or ""),
            chat_id=str(ctx.get("chat_id") or ""),
            contact_name=str(ctx.get("contact_name") or ""),
            conversation_mode=str(conversation_mode or "sales"),
            product_id=str(ctx.get("product_id") or ""),
        )
        for key, value in fallback.items():
            ctx.setdefault(key, value)
    except Exception:
        pass
    return ctx


def _compose_learning_context_block(prompt_context: dict[str, Any], conversation_mode: str) -> str:
    contact_profile = prompt_context.get("contact_profile") or {}
    product_profile = prompt_context.get("product_profile") or {}
    contact_text = json.dumps(contact_profile, ensure_ascii=False, sort_keys=True) if isinstance(contact_profile, dict) else str(contact_profile)
    product_text = json.dumps(product_profile, ensure_ascii=False, sort_keys=True) if isinstance(product_profile, dict) else str(product_profile)
    approved_playbook = str(prompt_context.get("approved_playbook") or "").strip()
    approved_faq = str(prompt_context.get("approved_faq") or "").strip()
    summary = str(prompt_context.get("conversation_summary") or "").strip()
    recent_history = _format_recent_history(prompt_context.get("recent_history"))
    product_block = format_product_analysis_block(product_profile if isinstance(product_profile, dict) else None)
    parts = [
        "Use somente o playbook aprovado, o FAQ aprovado, os dados do contato e o resumo curto abaixo.",
        f"Modo da conversa: {conversation_mode or 'sales'}",
        "Playbook aprovado:",
        approved_playbook or "Sem playbook aprovado carregado.",
        "FAQ aprovado:",
        approved_faq or "Sem FAQ aprovado carregado.",
        "Dados do contato:",
        contact_text or "{}",
        "Produto cadastrado:",
        product_text or "{}",
        "Resumo curto da conversa:",
        summary or "Sem resumo disponível.",
        "Histórico recente da conversa:",
        recent_history,
        "Nunca use a conversa bruta inteira no prompt.",
        "Se o cliente responder com palavras curtas como sim, ok, quero, manda ou preço, trate isso como continuidade natural do fluxo anterior.",
    ]
    if product_block:
        parts.extend([
            "Analise comercial do produto:",
            product_block,
            "Use estas especificacoes obrigatorias e diferenciais ao responder, sem inventar campos nao cadastrados.",
        ])
    return "\n\n".join(parts)


def _extract_text(data: dict[str, Any]) -> str:
    if isinstance(data, dict):
        value = data.get("output_text")
        if isinstance(value, str):
            return value.strip()

        parts = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts).strip()
    return ""


def _default_fallback_reply() -> str:
    return (
        "Oi! Como posso ajudar com o insumo que voce quer cotar?\n\n"
        "Eu posso te apoiar com especificacao tecnica, embalagem, aplicacao e condicao comercial.\n\n"
        "Voce quer me passar o produto ou a aplicacao final?"
    )
