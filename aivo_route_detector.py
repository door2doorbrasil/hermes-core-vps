from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gateway.whatsapp_identity import canonical_whatsapp_identifier
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

AIVO_SESSION_TTL_SECONDS = 10 * 24 * 60 * 60
AIVO_REMARKETING_DELAY_SECONDS = 3 * 24 * 60 * 60
AIVO_SESSION_STAGE = "aivo_sales"
AIVO_REMARKETING_STAGE = "aivo_remarketing"
AIVO_SESSION_KEY_PREFIX = "aivo_whatsapp_session:"
AIVO_REMARKETING_LINK = (
    "https://www.aivonote.com.br/discount/AIVONOTE10?redirect=%2Fproducts%2Fgravador-digital-com-inteligencia-artificial-6996073195503"
)
AIVO_REMARKETING_MESSAGE_DIRECT = (
    "Oi! Retomando seu interesse no AIVO Note: ele grava, transcreve e ajuda "
    "a organizar suas reuniões, aulas e entrevistas sem perder detalhes. "
    "Hoje você ainda pode aproveitar 10% de desconto neste link:\n\n"
    f"{AIVO_REMARKETING_LINK}\n\n"
    "Se fizer sentido para você, vale garantir agora."
)
AIVO_REMARKETING_MESSAGE_VENDEDOR = (
    "Oi! Vi que o AIVO Note fazia sentido pra você. Ele ajuda a gravar e "
    "organizar o conteúdo com muito mais praticidade, e hoje tem 10% de "
    "desconto para fechar com mais vantagem. Se quiser aproveitar, é por aqui:\n\n"
    f"{AIVO_REMARKETING_LINK}\n\n"
    "Se quiser, eu já te deixo no caminho mais rápido pra compra."
)
_AIVO_COLD_TRIGGER_PHRASES = (
    "aivo",
    "aivo note",
    "gravador",
    "gravador inteligente",
    "quero saber mais",
    "tenho interesse",
    "estou interessado",
    "preco",
    "preço",
    "valor",
    "quanto custa",
    "assinatura",
    "mensalidade",
    "anuidade",
    "planos",
    "comprar", "pagamento", "fazer pedido", "finalizar compra", "comprar agora", "quero comprar", "como eu compro", "como compro",
    "como funciona",
    "me mostra",
    "quero ver",
    "demonstracao",
    "demonstração",
    "video",
    "vídeo",
    "reuniao",
    "reunião",
    "aula",
    "entrevista",
    "atendimento",
    "transcricao",
    "transcrição",
    "resumo",
    "audio",
    "áudio",
    "facebook",
    "instagram",
    "anuncio",
    "anúncio",
    "anuncio do facebook",
    "anúncio do facebook",
)
_AIVO_SESSION_END_PHRASES = {
    "sair",
    "encerrar",
    "cancelar",
    "fim",
    "nao quero mais",
}

_AIVO_CONTINUATION_PHRASES = (
    "sim",
    "s",
    "ok",
    "pode",
    "quero",
    "manda",
    "envia",
    "me mostra",
    "mostra",
    "preco",
    "preço",
    "valor",
    "planos",
    "profissional",
    "presencial",
    "presenciais",
    "online",
    "aula",
    "aulas",
    "entrevista",
    "entrevistas",
    "atendimento",
    "atendimentos",
)


class AivoSessionStoreProtocol(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def clear(self, key: str) -> None: ...


@dataclass(slots=True)
class AivoRouteDecision:
    should_route: bool
    reason: str = ""
    text: str = ""
    sender_id: str = ""
    mode: str = "sales"


class AivoSessionStateStore:
    """Persist AIVO WhatsApp session state in Hermes state.db meta rows."""

    def __init__(self, db_path: Any = None):
        self.db_path = db_path
        self._db = None

    def _get_db(self):
        if self._db is not None:
            return self._db
        from hermes_state import SessionDB

        self._db = SessionDB(db_path=self.db_path) if self.db_path else SessionDB()
        return self._db

    def get(self, key: str) -> str:
        try:
            value = self._get_db().get_meta(key)
        except Exception as exc:
            logger.debug("AIVO session store get failed for %s: %s", key, exc)
            return ""
        return value or ""

    def set(self, key: str, value: Any) -> None:
        try:
            payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            self._get_db().set_meta(key, payload)
        except Exception as exc:
            logger.debug("AIVO session store set failed for %s: %s", key, exc)

    def clear(self, key: str) -> None:
        self.set(key, {"active": False, "cleared_at": _now()})


class AivoRouteDetector:
    """Helper that keeps AIVO WhatsApp routing state out of gateway.run."""

    def __init__(
        self,
        session_store: AivoSessionStoreProtocol | None = None,
        now_fn: Any = None,
    ) -> None:
        self.session_store = session_store or AivoSessionStateStore()
        self.now_fn = now_fn or time.time

    def extract_text(self, event: Any) -> str:
        for name in ("text", "message", "body", "content", "raw_text"):
            value = getattr(event, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(event, dict):
            for name in ("text", "message", "body", "content", "raw_text"):
                value = event.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def should_route_to_openai(self, event: Any, local_vars: dict[str, Any] | None = None) -> bool:
        return self.decision(event, local_vars).should_route

    def decision(self, event: Any, local_vars: dict[str, Any] | None = None) -> AivoRouteDecision:
        text = self.extract_text(event)
        normalized = _normalize_message(text)
        sender_id = self._resolve_sender_id(event, local_vars)
        chat_id = self._resolve_chat_id(event, local_vars)
        is_whatsapp = self._is_whatsapp_context(event, local_vars)
        now = _now(self.now_fn)

        if not normalized:
            return AivoRouteDecision(False, reason="empty_message", text=text, sender_id=sender_id)

        if not is_whatsapp:
            if _matches_phrase(normalized, _AIVO_COLD_TRIGGER_PHRASES):
                return AivoRouteDecision(False, reason="non_whatsapp_route", text=text, sender_id=sender_id)
            return AivoRouteDecision(False, reason="missing_aivo_trigger", text=text, sender_id=sender_id)

        session_key = self._session_key(sender_id)
        session = self._load_session(session_key)
        active = bool(session and session.get("active"))
        expired = bool(session and self._is_expired(session, now))
        stop_requested = _matches_end_phrase(normalized)
        trigger_requested = _matches_phrase(normalized, _AIVO_COLD_TRIGGER_PHRASES)

        if active and not expired:
            if stop_requested:
                self._end_session(session_key, session, now, ended_by_user=True)
                logger.info("AIVO session ended by user sender=%s", sender_id or "unknown")
                return AivoRouteDecision(False, reason="aivo_session_ended_by_user", text=text, sender_id=sender_id)

            mode = str(session.get("mode") or "sales").lower()
            if mode not in {"sales", "remarketing"}:
                mode = "remarketing" if session.get("remarketing_sent_at") else "sales"

            self._touch_session(
                session_key,
                session,
                now,
                chat_id=chat_id,
                mode=mode,
            )
            logger.info("AIVO session continued sender=%s", sender_id or "unknown")
            return AivoRouteDecision(
                True,
                reason="aivo_session_continued",
                text=text,
                sender_id=sender_id,
                mode=mode,
            )

        if session and expired:
            self._end_session(session_key, session, now, expired=True)
            logger.info("AIVO session expired sender=%s", sender_id or "unknown")

        if stop_requested:
            return AivoRouteDecision(False, reason="aivo_session_ended_by_user", text=text, sender_id=sender_id)

        if _has_aivo_context(local_vars):
            self._start_session(session_key, sender_id, chat_id, now, mode="sales")
            if _looks_like_aivo_followup(normalized):
                logger.info("AIVO session started from recent context sender=%s", sender_id or "unknown")
                reason = "aivo_session_started_from_history"
            else:
                logger.info("AIVO session started from history context sender=%s", sender_id or "unknown")
                reason = "aivo_session_started_from_context"
            return AivoRouteDecision(
                True,
                reason=reason,
                text=text,
                sender_id=sender_id,
                mode="sales",
            )

        if trigger_requested:
            self._start_session(session_key, sender_id, chat_id, now, mode="sales")
            logger.info("AIVO session started sender=%s", sender_id or "unknown")
            return AivoRouteDecision(True, reason="aivo_session_started", text=text, sender_id=sender_id, mode="sales")

        return AivoRouteDecision(False, reason="missing_aivo_trigger", text=text, sender_id=sender_id)

    def _session_key(self, sender_id: str) -> str:
        return f"{AIVO_SESSION_KEY_PREFIX}{sender_id or 'unknown'}"

    def _load_session(self, session_key: str) -> dict[str, Any] | None:
        raw = self.session_store.get(session_key)
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                return None
            return data if isinstance(data, dict) else None
        return None

    def _save_session(self, session_key: str, payload: dict[str, Any]) -> None:
        self.session_store.set(session_key, json.dumps(payload, ensure_ascii=False))

    def _start_session(
        self,
        session_key: str,
        sender_id: str,
        chat_id: str,
        now: float,
        *,
        mode: str = "sales",
    ) -> None:
        payload = {
            "active": True,
            "mode": mode,
            "stage": AIVO_SESSION_STAGE if mode == "sales" else AIVO_REMARKETING_STAGE,
            "sender_id": sender_id,
            "chat_id": chat_id,
            "started_at": now,
            "last_customer_at": now,
            "updated_at": now,
            "expires_at": now + AIVO_SESSION_TTL_SECONDS,
            "remarketing_due_at": now + AIVO_REMARKETING_DELAY_SECONDS,
            "remarketing_sent_at": None,
        }
        self._save_session(session_key, payload)

    def _touch_session(
        self,
        session_key: str,
        session: dict[str, Any],
        now: float,
        *,
        chat_id: str,
        mode: str = "sales",
    ) -> None:
        started_at = float(session.get("started_at") or now)
        payload = {
            "active": True,
            "mode": mode,
            "stage": session.get("stage") or (AIVO_SESSION_STAGE if mode == "sales" else AIVO_REMARKETING_STAGE),
            "sender_id": session.get("sender_id") or "",
            "chat_id": chat_id or session.get("chat_id") or "",
            "started_at": started_at,
            "last_customer_at": now,
            "updated_at": now,
            "expires_at": now + AIVO_SESSION_TTL_SECONDS,
            "remarketing_due_at": now + AIVO_REMARKETING_DELAY_SECONDS,
            "remarketing_sent_at": session.get("remarketing_sent_at"),
        }
        self._save_session(session_key, payload)

    def _end_session(self, session_key: str, session: dict[str, Any], now: float, *, ended_by_user: bool = False, expired: bool = False) -> None:
        payload = {
            "active": False,
            "mode": session.get("mode") or "sales",
            "stage": session.get("stage") or AIVO_SESSION_STAGE,
            "sender_id": session.get("sender_id") or "",
            "chat_id": session.get("chat_id") or "",
            "started_at": session.get("started_at"),
            "last_customer_at": session.get("last_customer_at"),
            "updated_at": now,
            "ended_at": now if ended_by_user else session.get("ended_at"),
            "expired_at": now if expired else session.get("expired_at"),
            "remarketing_due_at": session.get("remarketing_due_at"),
            "remarketing_sent_at": session.get("remarketing_sent_at"),
        }
        self._save_session(session_key, payload)

    def _is_expired(self, session: dict[str, Any], now: float) -> bool:
        expires_at = _coerce_float(session.get("expires_at"))
        if expires_at is None:
            base = _coerce_float(session.get("last_customer_at") or session.get("started_at"))
            if base is None:
                return True
            expires_at = base + AIVO_SESSION_TTL_SECONDS
        return now >= expires_at

    def _resolve_sender_id(self, event: Any, local_vars: dict[str, Any] | None = None) -> str:
        candidates: list[str] = []
        source = None
        if local_vars:
            source = local_vars.get("source")
            if source is None:
                source = local_vars.get("event_source")
        if source is not None:
            for name in ("user_id", "user_id_alt", "chat_id", "participant_id", "sender_id"):
                value = getattr(source, name, None)
                if value:
                    candidates.append(str(value))
        for name in ("user_id", "user_id_alt", "chat_id", "participant_id", "sender_id"):
            value = getattr(event, name, None)
            if value:
                candidates.append(str(value))
            elif isinstance(event, dict):
                value = event.get(name)
                if value:
                    candidates.append(str(value))
        for candidate in candidates:
            normalized = canonical_whatsapp_identifier(candidate)
            if normalized:
                return normalized
        return ""

    def _resolve_chat_id(self, event: Any, local_vars: dict[str, Any] | None = None) -> str:
        source = None
        if local_vars:
            source = local_vars.get("source")
        if source is not None:
            for name in ("chat_id", "chatId"):
                value = getattr(source, name, None)
                if value:
                    return str(value)
        for name in ("chat_id", "chatId"):
            value = getattr(event, name, None)
            if value:
                return str(value)
            if isinstance(event, dict):
                value = event.get(name)
                if value:
                    return str(value)
        return ""

    def _is_whatsapp_context(self, event: Any, local_vars: dict[str, Any] | None = None) -> bool:
        platform = None
        if local_vars:
            source = local_vars.get("source")
            platform = getattr(source, "platform", None) if source is not None else local_vars.get("platform")
        if platform is None:
            platform = getattr(event, "platform", None)
        platform_value = str(getattr(platform, "value", platform) or "").strip().lower()
        return "whatsapp" in platform_value


def _has_aivo_context(local_vars: dict[str, Any] | None = None) -> bool:
    if not local_vars:
        return False
    history = local_vars.get("history")
    if not history:
        return False

    items: list[str] = []
    try:
        iterable = list(history)[-8:]
    except Exception:
        return False

    for entry in iterable:
        if isinstance(entry, dict):
            for key in ("content", "text", "message", "body"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    items.append(value.strip())
                    break
        elif isinstance(entry, str) and entry.strip():
            items.append(entry.strip())

    if not items:
        return False

    joined = _normalize_message(" ".join(items))
    if _matches_phrase(joined, _AIVO_COLD_TRIGGER_PHRASES):
        return True
    if _looks_like_aivo_followup(joined):
        return True
    return False


def _looks_like_aivo_followup(normalized_text: str) -> bool:
    if not normalized_text:
        return False
    return _matches_phrase(normalized_text, _AIVO_CONTINUATION_PHRASES)


_DEFAULT_DETECTOR = AivoRouteDetector()


def extract_aivo_text(event: Any) -> str:
    return _DEFAULT_DETECTOR.extract_text(event)


def should_route_to_aivo_openai(event: Any, local_vars: dict[str, Any] | None = None) -> bool:
    return _DEFAULT_DETECTOR.should_route_to_openai(event, local_vars)


def aivo_route_decision(event: Any, local_vars: dict[str, Any] | None = None) -> AivoRouteDecision:
    return _DEFAULT_DETECTOR.decision(event, local_vars)


def iter_aivo_sessions(db_path: Path | None = None) -> list[tuple[str, dict[str, Any]]]:
    db_path = db_path or (_default_db_path())
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?",
            (f"{AIVO_SESSION_KEY_PREFIX}%",),
        ).fetchall()
    finally:
        conn.close()

    sessions: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        key = str(row["key"])
        raw = row["value"]
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if isinstance(payload, dict):
            sessions.append((key, payload))
    return sessions


def mark_aivo_remarketing_sent(sender_id: str, now: float | None = None, db_path: Path | None = None) -> bool:
    db_path = db_path or _default_db_path()
    session_key = f"{AIVO_SESSION_KEY_PREFIX}{sender_id or 'unknown'}"
    store = AivoSessionStateStore(db_path=db_path)
    session = _load_aivo_session_value(store.get(session_key))
    if not session:
        return False
    now_value = _now(now)
    session["mode"] = "remarketing"
    session["stage"] = AIVO_REMARKETING_STAGE
    session["remarketing_sent_at"] = now_value
    session["updated_at"] = now_value
    store.set(session_key, session)
    return True


def select_aivo_remarketing_sessions(now: float | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    now_value = _now(now)
    due: list[dict[str, Any]] = []
    for key, session in iter_aivo_sessions(db_path=db_path):
        if not session.get("active"):
            continue
        if session.get("remarketing_sent_at"):
            continue
        if _is_aivo_session_expired(session, now_value):
            continue
        due_at = _coerce_float(session.get("remarketing_due_at"))
        if due_at is None:
            base = _coerce_float(session.get("last_customer_at") or session.get("started_at"))
            if base is None:
                continue
            due_at = base + AIVO_REMARKETING_DELAY_SECONDS
        if now_value >= due_at:
            record = dict(session)
            record["session_key"] = key
            due.append(record)
    return due


def build_aivo_remarketing_message(style: str = "vendedor") -> str:
    style = (style or "vendedor").strip().lower()
    if style == "direct":
        return AIVO_REMARKETING_MESSAGE_DIRECT
    return AIVO_REMARKETING_MESSAGE_VENDEDOR


def _default_db_path() -> Path:
    return get_hermes_home() / "state.db"


def _load_aivo_session_value(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _now(now_fn: Any = None) -> float:
    try:
        if now_fn is None:
            return time.time()
        if callable(now_fn):
            return float(now_fn())
        return float(now_fn)
    except Exception:
        return time.time()


def _normalize_message(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _matches_phrase(normalized_text: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        candidate = _normalize_message(phrase)
        if not candidate:
            continue
        if " " in candidate:
            if candidate in normalized_text:
                return True
        elif re.search(rf"\b{re.escape(candidate)}\b", normalized_text):
            return True
    return False


def _matches_end_phrase(normalized_text: str) -> bool:
    return bool(re.search(r"\b(?:sair|encerrar|cancelar|fim|nao quero mais)\b", normalized_text))


def _is_aivo_session_expired(session: dict[str, Any], now: float) -> bool:
    expires_at = _coerce_float(session.get("expires_at"))
    if expires_at is None:
        base = _coerce_float(session.get("last_customer_at") or session.get("started_at"))
        if base is None:
            return True
        expires_at = base + AIVO_SESSION_TTL_SECONDS
    return now >= expires_at
