from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from agent.secret_scope import get_secret
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

DEFAULT_PRODUCT_ANALYSIS_MODEL = "gpt-4.1-mini"
DEFAULT_PRODUCT_ANALYSIS_TEMPERATURE = 0.2
DEFAULT_PRODUCT_ANALYSIS_TOKENS = 900


class ProductAnalysisConfigurationError(RuntimeError):
    """Raised when the product analysis helper cannot be configured."""


class ProductAnalysisError(RuntimeError):
    """Raised when the product analysis helper receives a bad response."""


@dataclass(slots=True)
class ProductSalesAnalysis:
    product_id: str
    product_name: str
    required_sale_specs: list[str] = field(default_factory=list)
    mandatory_sale_fields: list[str] = field(default_factory=list)
    market_differentiators: list[str] = field(default_factory=list)
    transactional_sell_points: list[str] = field(default_factory=list)
    customer_questions: list[str] = field(default_factory=list)
    avoid_promises: list[str] = field(default_factory=list)
    positioning: str = ""
    short_pitch: str = ""
    confidence: str = "medium"
    model: str = DEFAULT_PRODUCT_ANALYSIS_MODEL
    raw_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "required_sale_specs": list(self.required_sale_specs),
            "mandatory_sale_fields": list(self.mandatory_sale_fields),
            "market_differentiators": list(self.market_differentiators),
            "transactional_sell_points": list(self.transactional_sell_points),
            "customer_questions": list(self.customer_questions),
            "avoid_promises": list(self.avoid_promises),
            "positioning": self.positioning,
            "short_pitch": self.short_pitch,
            "confidence": self.confidence,
            "model": self.model,
            "raw_summary": self.raw_summary,
        }


def analyze_product_for_sales(product: dict[str, Any]) -> ProductSalesAnalysis:
    """Analyze a product with ChatGPT and return a sales-oriented summary.

    The helper prefers the OpenAI Responses API. If the runtime is not
    configured, it falls back to a deterministic rule-based summary so the
    product catalog remains usable in local/test environments.
    """
    normalized = _normalize_product_payload(product)
    try:
        client = _SalesProductAnalyzer.from_runtime()
    except ProductAnalysisConfigurationError:
        return _fallback_analysis(normalized)
    try:
        return client.analyze(normalized)
    except Exception:
        logger.debug("product analysis failed, using fallback", exc_info=True)
        return _fallback_analysis(normalized)


def format_product_analysis_block(analysis: dict[str, Any] | ProductSalesAnalysis | None) -> str:
    if not analysis:
        return ""
    data = analysis.to_dict() if isinstance(analysis, ProductSalesAnalysis) else dict(analysis)
    nested = data.get("analysis")
    if isinstance(nested, dict):
        merged = dict(data)
        merged.update(nested)
        data = merged

    def _bulletize(values: Any) -> str:
        if isinstance(values, list) and values:
            return "\n".join(f"- {str(item).strip()}" for item in values if str(item).strip())
        if isinstance(values, str) and values.strip():
            return f"- {values.strip()}"
        return "- Nao informado"

    parts = [
        "Analise do produto para vendas:",
        f"Produto: {data.get('product_name') or data.get('product_id') or 'Nao informado'}",
        "Especificacoes obrigatorias para informar na venda:",
        _bulletize(data.get("mandatory_sale_fields") or data.get("required_sale_specs")),
        "Especificacoes obrigatorias para citar:",
        _bulletize(data.get("required_sale_specs") or data.get("mandatory_sale_fields")),
        "Diferenciais para mercado transacional:",
        _bulletize(data.get("market_differentiators")),
        "Argumentos de venda transacional:",
        _bulletize(data.get("transactional_sell_points")),
        "Perguntas que aumentam a chance de fechamento:",
        _bulletize(data.get("customer_questions")),
    ]
    positioning = str(data.get("positioning") or "").strip()
    short_pitch = str(data.get("short_pitch") or "").strip()
    if positioning:
        parts.extend(["Posicionamento:", positioning])
    if short_pitch:
        parts.extend(["Pitch curto:", short_pitch])
    avoid_promises = data.get("avoid_promises") or []
    if avoid_promises:
        parts.extend(["Evitar promessas:", _bulletize(avoid_promises)])
    return "\n\n".join(part for part in parts if str(part).strip())


class _SalesProductAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float,
        base_url: str = "",
        timeout: float = 45.0,
        max_output_tokens: int = DEFAULT_PRODUCT_ANALYSIS_TOKENS,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.base_url = base_url
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    @classmethod
    def from_runtime(cls) -> "_SalesProductAnalyzer":
        cfg = load_config()
        model = _first_nonempty(
            cfg_get(cfg, "aivo", "product_model"),
            cfg_get(cfg, "aivo", "analysis_model"),
            cfg_get(cfg, "model", "default"),
            os.getenv("AIVO_PRODUCT_ANALYSIS_MODEL"),
            os.getenv("AIVO_OPENAI_MODEL"),
            DEFAULT_PRODUCT_ANALYSIS_MODEL,
        )
        temperature_raw = _first_nonempty(
            cfg_get(cfg, "aivo", "product_temperature"),
            cfg_get(cfg, "aivo", "analysis_temperature"),
            os.getenv("AIVO_PRODUCT_ANALYSIS_TEMPERATURE"),
            os.getenv("AIVO_OPENAI_TEMPERATURE"),
            DEFAULT_PRODUCT_ANALYSIS_TEMPERATURE,
        )
        try:
            temperature = float(temperature_raw)
        except Exception as exc:
            raise ProductAnalysisConfigurationError(
                f"Invalid product analysis temperature value: {temperature_raw!r}"
            ) from exc

        api_key = _resolve_api_key(cfg)
        if not api_key:
            raise ProductAnalysisConfigurationError(
                "OpenAI API key not found in Hermes config or environment."
            )

        base_url = _first_nonempty(
            cfg_get(cfg, "aivo", "product_base_url"),
            cfg_get(cfg, "aivo", "analysis_base_url"),
            cfg_get(cfg, "model", "base_url"),
            os.getenv("AIVO_PRODUCT_ANALYSIS_BASE_URL"),
            os.getenv("OPENAI_BASE_URL"),
            "",
        )
        timeout_raw = _first_nonempty(
            cfg_get(cfg, "aivo", "product_timeout"),
            cfg_get(cfg, "aivo", "analysis_timeout"),
            os.getenv("AIVO_PRODUCT_ANALYSIS_TIMEOUT"),
            os.getenv("OPENAI_TIMEOUT"),
            45.0,
        )
        try:
            timeout = float(timeout_raw)
        except Exception:
            timeout = 45.0

        max_output_tokens = _coerce_int(
            _first_nonempty(
                cfg_get(cfg, "aivo", "product_max_output_tokens"),
                cfg_get(cfg, "aivo", "analysis_max_output_tokens"),
                DEFAULT_PRODUCT_ANALYSIS_TOKENS,
            ),
            default=DEFAULT_PRODUCT_ANALYSIS_TOKENS,
        )
        return cls(
            api_key=api_key,
            model=str(model).strip(),
            temperature=temperature,
            base_url=str(base_url).strip(),
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )

    def analyze(self, product: dict[str, Any]) -> ProductSalesAnalysis:
        payload = {
            "model": self.model,
            "instructions": _build_instructions(),
            "input": _compose_input(product),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        response = self._request(payload)
        text = _extract_text(response)
        data = _parse_json_analysis(text)
        analysis = ProductSalesAnalysis(
            product_id=str(product.get("product_id") or ""),
            product_name=str(product.get("name") or product.get("product_name") or ""),
            required_sale_specs=_clean_list(data.get("required_sale_specs") or data.get("required_specs")),
            mandatory_sale_fields=_clean_list(data.get("mandatory_sale_fields") or data.get("mandatory_fields")),
            market_differentiators=_clean_list(data.get("market_differentiators") or data.get("differentials")),
            transactional_sell_points=_clean_list(data.get("transactional_sell_points") or data.get("sell_points")),
            customer_questions=_clean_list(data.get("customer_questions") or data.get("questions")),
            avoid_promises=_clean_list(data.get("avoid_promises") or data.get("risks")),
            positioning=str(data.get("positioning") or data.get("market_positioning") or "").strip(),
            short_pitch=str(data.get("short_pitch") or data.get("pitch") or "").strip(),
            confidence=str(data.get("confidence") or "medium").strip() or "medium",
            model=self.model,
            raw_summary=text.strip(),
        )
        if not analysis.required_sale_specs:
            analysis.required_sale_specs = _fallback_required_specs(product)
        if not analysis.mandatory_sale_fields:
            analysis.mandatory_sale_fields = list(analysis.required_sale_specs)
        if not analysis.market_differentiators:
            analysis.market_differentiators = _fallback_differentiators(product)
        if not analysis.transactional_sell_points:
            analysis.transactional_sell_points = _fallback_sell_points(product)
        if not analysis.customer_questions:
            analysis.customer_questions = _fallback_questions(product)
        if not analysis.positioning:
            analysis.positioning = _fallback_positioning(product)
        if not analysis.short_pitch:
            analysis.short_pitch = _fallback_pitch(product)
        return analysis

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
            raise ProductAnalysisError(f"OpenAI HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise ProductAnalysisError(f"OpenAI request failed: {exc}") from exc
        except Exception as exc:
            raise ProductAnalysisError(f"OpenAI request failed: {exc}") from exc


def _build_instructions() -> str:
    return (
        "Voce e um analista senior de insumos industriais, materias-primas e vendas transacionais B2B.\n"
        "Leia o cadastro do produto e retorne APENAS JSON valido, sem markdown.\n"
        "Objetivo: identificar as especificacoes obrigatorias que a equipe deve informar na venda, "
        "os campos que precisam ser confirmados com o cliente, e os diferenciais que aumentam a "
        "probabilidade de fechamento no mercado transacional de insumos, aditivos, amidos, feculas, "
        "bicarbonatos e materias-primas similares.\n"
        "Regras de saida:\n"
        "- Use somente as chaves: required_sale_specs, mandatory_sale_fields, market_differentiators, "
        "transactional_sell_points, customer_questions, avoid_promises, positioning, short_pitch, confidence.\n"
        "- Cada chave de lista deve ser uma lista de strings curtas.\n"
        "- Diga apenas o que pode ser usado comercialmente sem inventar especificacoes.\n"
        "- Se houver lacunas no cadastro, use a lista avoid_promises para apontar o que nao deve ser afirmado.\n"
        "- Em positioning, resuma como vender o produto em uma frase.\n"
        "- Em short_pitch, escreva uma frase curta de venda.\n"
    )


def _compose_input(product: dict[str, Any]) -> str:
    safe = {
        "product_id": str(product.get("product_id") or ""),
        "name": str(product.get("name") or product.get("product_name") or ""),
        "category": str(product.get("category") or ""),
        "description": str(product.get("description") or ""),
        "target_audience": str(product.get("target_audience") or ""),
        "use_case": str(product.get("use_case") or ""),
        "market": str(product.get("market") or ""),
        "price": str(product.get("price") or ""),
        "features": _clean_list(product.get("features")),
        "specs": _clean_list(product.get("specs")),
        "constraints": _clean_list(product.get("constraints")),
        "notes": str(product.get("notes") or ""),
    }
    return json.dumps(safe, ensure_ascii=False, indent=2)


def _parse_json_analysis(text: str) -> dict[str, Any]:
    if not text:
        return {}
    candidate = text.strip()
    try:
        payload = json.loads(candidate)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    match = re.search(r"\{.*\}", candidate, re.S)
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
    return {}


def _extract_text(data: dict[str, Any]) -> str:
    if isinstance(data, dict):
        value = data.get("output_text")
        if isinstance(value, str):
            return value.strip()
        parts: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts).strip()
    return ""


def _normalize_product_payload(product: dict[str, Any]) -> dict[str, Any]:
    data = dict(product or {})
    data.setdefault("product_id", data.get("id") or data.get("slug") or "")
    data.setdefault("name", data.get("product_name") or data.get("title") or "")
    return data


def _resolve_api_key(cfg: dict[str, Any] | None) -> str:
    candidates: Iterable[str] = (
        str(cfg_get(cfg, "aivo", "product_api_key") or "").strip(),
        str(cfg_get(cfg, "aivo", "analysis_api_key") or "").strip(),
        str(cfg_get(cfg, "model", "api_key") or "").strip(),
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


def _clean_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        cleaned = values.strip()
        return [cleaned] if cleaned else []
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _fallback_required_specs(product: dict[str, Any]) -> list[str]:
    specs = []
    for key in (
        "price",
        "sku",
        "reference",
        "brand",
        "model",
        "purity",
        "granulometry",
        "particle_size",
        "moisture",
        "ph",
        "composition",
        "material",
        "packaging",
        "warranty",
    ):
        if str(product.get(key) or "").strip():
            specs.append(f"{key}: {str(product.get(key)).strip()}")
    for item in _clean_list(product.get("specs"))[:6]:
        specs.append(item)
    return specs or [
        "identificacao tecnica do insumo",
        "especificacoes de qualidade",
        "condicao de embalagem",
        "faixa de aplicacao",
    ]


def _fallback_differentiators(product: dict[str, Any]) -> list[str]:
    features = _clean_list(product.get("features"))
    if features:
        return features[:5]
    description = str(product.get("description") or "").strip()
    if description:
        return [description[:120]]
    return [
        "pureza e consistencia do lote",
        "padrao tecnico confiavel",
        "estabilidade de fornecimento",
        "aplicacao industrial clara",
    ]


def _fallback_sell_points(product: dict[str, Any]) -> list[str]:
    points = []
    for item in _fallback_required_specs(product)[:3]:
        points.append(f"Citar {item}")
    for item in _fallback_differentiators(product)[:3]:
        points.append(f"Explorar {item}")
    return points[:6]


def _fallback_questions(product: dict[str, Any]) -> list[str]:
    questions = []
    if not str(product.get("target_audience") or "").strip():
        questions.append("Quem e o publico comprador ideal?")
    if not str(product.get("use_case") or "").strip():
        questions.append("Em qual aplicacao industrial ou alimenticia o produto ganha mais forca?")
    if not str(product.get("price") or "").strip():
        questions.append("Qual a faixa de preco ou condicao comercial?")
    if not _clean_list(product.get("specs")):
        questions.append("Quais especificacoes tecnicas precisam ser confirmadas?")
    if not str(product.get("packaging") or "").strip():
        questions.append("Qual embalagem, peso liquido e formato de expedicao?")
    return questions or ["O que mais pesa na decisao de compra?"]


def _fallback_positioning(product: dict[str, Any]) -> str:
    name = str(product.get("name") or product.get("product_name") or "o produto").strip()
    use_case = str(product.get("use_case") or product.get("description") or "resolver uma dor clara").strip()
    return f"Vender {name} destacando especificacao tecnica, estabilidade de lote e aderencia ao uso final."


def _fallback_pitch(product: dict[str, Any]) -> str:
    name = str(product.get("name") or product.get("product_name") or "o produto").strip()
    return f"{name}: foco em especificacao, confianca de fornecimento e fechamento rapido."
