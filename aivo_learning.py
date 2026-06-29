from __future__ import annotations

import json
import os
import hashlib
import tempfile
import threading
from textwrap import dedent
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from business.aivo.product_analysis import (
    ProductSalesAnalysis,
    analyze_product_for_sales,
    format_product_analysis_block,
)

LEARNING_ROOT_ENV = 'AIVO_LEARNING_ROOT'
DEFAULT_LEARNING_ROOT = Path('/opt/data/aivo-learning')
CONTACTS_FILE = 'contacts.jsonl'
CONVERSATIONS_FILE = 'conversations.jsonl'
OBJECTIONS_FILE = 'objections.jsonl'
LEARNING_QUEUE_FILE = 'learning_queue.jsonl'
APPROVED_PLAYBOOK_FILE = 'approved_playbook.md'
FAQ_APPROVED_FILE = 'faq_approved.md'
LEARNING_REVIEW_FILE = 'learning_reviews.jsonl'
PRODUCTS_FILE = 'products.jsonl'

KNOWN_INTENTS = {'pricing', 'purchase', 'remarketing', 'demo', 'how_it_works', 'support', 'objection', 'comparison', 'follow_up', 'other'}
KNOWN_OBJECTIONS = {'price', 'app_dependency', 'privacy', 'trust', 'comparison', 'time', 'quality', 'other'}
SAFE_PROMOTABLE_OBJECTIONS = {'app_dependency', 'privacy', 'trust', 'quality'}
COMMERCIAL_LOCK_TERMS = {
    'preco',
    'precos',
    'plano',
    'planos',
    'assinatura',
    'mensalidade',
    'anuidade',
    'juros',
    'parcelado',
    'parcelamento',
    'garantia',
    'reembolso',
    'cancelamento',
    'desconto',
    'promocao',
    'checkout',
    'pagamento',
    'cartao',
    'parcelas',
    'parcela',
    'credito',
    'cobranca',
    'politica',
    'politicas',
    'condicao',
    'condicoes',
}

_learning_lock = threading.Lock()


@dataclass(slots=True)
class AivoLearningContext:
    contact_id: str
    sender_id: str = ''
    chat_id: str = ''
    contact_name: str = ''
    language: str = 'pt-BR'
    conversation_mode: str = 'sales'
    approved_playbook: str = ''
    approved_faq: str = ''
    product_profile: dict[str, Any] = field(default_factory=dict)
    contact_profile: dict[str, Any] = field(default_factory=dict)
    conversation_summary: str = ''


class AivoLearningStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or learning_root()
        self.ensure()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for filename in (CONTACTS_FILE, CONVERSATIONS_FILE, OBJECTIONS_FILE, LEARNING_QUEUE_FILE, LEARNING_REVIEW_FILE, PRODUCTS_FILE):
            path = self.root / filename
            if not path.exists():
                path.write_text('', encoding='utf-8')
        playbook = self.root / APPROVED_PLAYBOOK_FILE
        if not playbook.exists():
            playbook.write_text(default_playbook(), encoding='utf-8')
        faq = self.root / FAQ_APPROVED_FILE
        if not faq.exists():
            faq.write_text(default_faq(), encoding='utf-8')

    def load_contact_profile(self, contact_id: str) -> dict[str, Any]:
        latest: dict[str, Any] | None = None
        for record in self._iter_jsonl(CONTACTS_FILE):
            if str(record.get('contact_id') or '') == contact_id:
                latest = record
        return latest or {'contact_id': contact_id, 'message_count': 0, 'summary': '', 'language': 'pt-BR'}

    def load_context(self, *, contact_id: str, sender_id: str = '', chat_id: str = '', contact_name: str = '', conversation_mode: str = 'sales', product_id: str = '') -> AivoLearningContext:
        profile = self.load_contact_profile(contact_id)
        product_id = product_id or str(profile.get('product_id') or '')
        product_profile = self.load_product_profile(product_id) if product_id else {}
        return AivoLearningContext(
            contact_id=contact_id,
            sender_id=sender_id,
            chat_id=chat_id,
            contact_name=contact_name or str(profile.get('contact_name') or ''),
            language=str(profile.get('language') or 'pt-BR'),
            conversation_mode=conversation_mode,
            approved_playbook=self._read_text(APPROVED_PLAYBOOK_FILE),
            approved_faq=self._read_text(FAQ_APPROVED_FILE),
            product_profile=product_profile,
            contact_profile=profile,
            conversation_summary=str(profile.get('summary') or ''),
        )

    def build_prompt_context(self, *, contact_id: str, sender_id: str = '', chat_id: str = '', contact_name: str = '', conversation_mode: str = 'sales', product_id: str = '', recent_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        ctx = self.load_context(contact_id=contact_id, sender_id=sender_id, chat_id=chat_id, contact_name=contact_name, conversation_mode=conversation_mode, product_id=product_id)
        profile = dict(ctx.contact_profile)
        profile.setdefault('contact_name', ctx.contact_name)
        profile.setdefault('language', ctx.language)
        return {
            'approved_playbook': ctx.approved_playbook,
            'approved_faq': ctx.approved_faq,
            'product_profile': ctx.product_profile,
            'contact_profile': profile,
            'conversation_summary': ctx.conversation_summary,
            'contact_id': ctx.contact_id,
            'sender_id': ctx.sender_id,
            'chat_id': ctx.chat_id,
            'contact_name': ctx.contact_name,
            'conversation_mode': ctx.conversation_mode,
            'product_id': product_id,
            'recent_history': recent_history or [],
        }

    def record_turn(self, *, contact_id: str, sender_id: str = '', chat_id: str = '', contact_name: str = '', user_text: str = '', assistant_text: str = '', conversation_mode: str = 'sales', platform: str = 'whatsapp', product_id: str = '') -> dict[str, Any]:
        now = time.time()
        user_text = (user_text or '').strip()
        assistant_text = (assistant_text or '').strip()
        intent = detect_intent(user_text, assistant_text, conversation_mode=conversation_mode)
        objection = detect_objection(user_text)
        next_step = detect_next_step(user_text, assistant_text, intent=intent, objection=objection)
        summary = summarize_turn(user_text, assistant_text, intent=intent, objection=objection, next_step=next_step)
        profile = self.load_contact_profile(contact_id)
        product_id = product_id or str(profile.get('product_id') or '')
        product_profile = self.load_product_profile(product_id) if product_id else {}
        merged_summary = merge_summary(str(profile.get('summary') or ''), summary)
        tags = update_tags(profile.get('tags') or [], intent=intent, objection=objection, next_step=next_step)
        updated_profile = {
            'contact_id': contact_id,
            'sender_id': sender_id or str(profile.get('sender_id') or ''),
            'chat_id': chat_id or str(profile.get('chat_id') or ''),
            'contact_name': contact_name or str(profile.get('contact_name') or ''),
            'language': profile.get('language') or 'pt-BR',
            'conversation_mode': conversation_mode,
            'product_id': product_id or str(profile.get('product_id') or ''),
            'summary': merged_summary,
            'last_intent': intent,
            'last_objection': objection,
            'last_next_step': next_step,
            'message_count': int(profile.get('message_count') or 0) + 1,
            'first_seen_at': profile.get('first_seen_at') or now,
            'last_seen_at': now,
            'tags': tags,
        }
        self.append_jsonl(CONTACTS_FILE, updated_profile)
        self.append_jsonl(CONVERSATIONS_FILE, {
            'ts': now,
            'platform': platform,
            'contact_id': contact_id,
            'sender_id': sender_id,
            'chat_id': chat_id,
            'contact_name': contact_name,
            'conversation_mode': conversation_mode,
            'product_id': product_id,
            'user_text': user_text,
            'assistant_text': assistant_text,
            'intent': intent,
            'objection': objection,
            'next_step': next_step,
            'summary': summary,
        })
        if objection != 'other':
            self.append_jsonl(OBJECTIONS_FILE, {
                'ts': now,
                'contact_id': contact_id,
                'sender_id': sender_id,
                'chat_id': chat_id,
                'contact_name': contact_name,
                'platform': platform,
                'product_id': product_id,
                'intent': intent,
                'objection': objection,
                'sample_user_text': user_text,
                'sample_assistant_text': assistant_text,
            })
        suggestion = build_learning_suggestion(
            contact_id=contact_id,
            sender_id=sender_id,
            chat_id=chat_id,
            contact_name=contact_name,
            intent=intent,
            objection=objection,
            next_step=next_step,
            user_text=user_text,
            assistant_text=assistant_text,
            conversation_mode=conversation_mode,
        )
        if suggestion:
            self.append_jsonl(LEARNING_QUEUE_FILE, suggestion)
            if _autopromote_learning_enabled():
                self.promote_learning_queue()
        return {'contact_profile': updated_profile, 'product_profile': product_profile, 'conversation_summary': merged_summary, 'intent': intent, 'objection': objection, 'next_step': next_step, 'learning_suggestion': suggestion}

    def upsert_product(
        self,
        *,
        product_id: str,
        name: str,
        category: str = '',
        description: str = '',
        target_audience: str = '',
        use_case: str = '',
        market: str = '',
        price: str = '',
        features: list[str] | None = None,
        specs: list[str] | None = None,
        constraints: list[str] | None = None,
        notes: str = '',
        source_url: str = '',
        analyze: bool = True,
        analysis_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        product_id = (product_id or '').strip()
        name = (name or '').strip()
        if not product_id:
            raise ValueError('product_id is required')
        if not name:
            raise ValueError('name is required')
        now = time.time()
        record = {
            'product_id': product_id,
            'name': name,
            'category': category,
            'description': description,
            'target_audience': target_audience,
            'use_case': use_case,
            'market': market,
            'price': price,
            'features': list(features or []),
            'specs': list(specs or []),
            'constraints': list(constraints or []),
            'notes': notes,
            'source_url': source_url,
            'created_at': now,
            'updated_at': now,
        }
        if analysis_override is not None:
            record['analysis'] = analysis_override
        elif analyze:
            analysis = analyze_product_for_sales(record)
            record['analysis'] = analysis.to_dict()
        else:
            record['analysis'] = {}
        self.append_jsonl(PRODUCTS_FILE, record)
        return record

    def load_product_profile(self, product_id: str) -> dict[str, Any]:
        latest: dict[str, Any] | None = None
        for record in self._iter_jsonl(PRODUCTS_FILE):
            if str(record.get('product_id') or '') == product_id:
                latest = record
        return latest or {}

    def list_products(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for record in self._iter_jsonl(PRODUCTS_FILE):
            product_id = str(record.get('product_id') or '').strip()
            if not product_id:
                continue
            if product_id not in latest:
                order.append(product_id)
            latest[product_id] = record
        return [latest[product_id] for product_id in order]

    def append_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write('\n')

    def _read_text(self, filename: str) -> str:
        try:
            return (self.root / filename).read_text(encoding='utf-8').strip()
        except OSError:
            return ''

    def _iter_jsonl(self, filename: str):
        path = self.root / filename
        if not path.exists():
            return
        for line in path.read_text(encoding='utf-8').splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload

    def _read_review_hashes(self) -> set[str]:
        hashes: set[str] = set()
        for record in self._iter_jsonl(LEARNING_REVIEW_FILE):
            if str(record.get('status') or '') != 'applied':
                continue
            fingerprint = str(record.get('fingerprint') or '').strip()
            if fingerprint:
                hashes.add(fingerprint)
        return hashes

    def promote_learning_queue(self, *, min_hits: int = 2) -> dict[str, Any]:
        with _learning_lock:
            queue = [record for record in self._iter_jsonl(LEARNING_QUEUE_FILE) if str(record.get('status') or '') == 'pending_review']
            if not queue:
                return {'applied': 0, 'skipped': 0, 'reasons': []}

            grouped: dict[str, list[dict[str, Any]]] = {}
            for record in queue:
                key = _learning_promotion_key(record)
                if not key:
                    continue
                grouped.setdefault(key, []).append(record)

            applied_hashes = self._read_review_hashes()
            applied = 0
            skipped = 0
            reasons: list[dict[str, Any]] = []
            for key, records in grouped.items():
                proposal = _build_learning_promotion(key, records)
                if proposal is None:
                    skipped += 1
                    reasons.append({'key': key, 'status': 'skipped', 'reason': 'unsafe_or_unsupported'})
                    continue
                if len(records) < max(1, min_hits):
                    skipped += 1
                    reasons.append({'key': key, 'status': 'skipped', 'reason': f'needs_{min_hits}_hits'})
                    continue
                fingerprint = proposal['fingerprint']
                if fingerprint in applied_hashes:
                    skipped += 1
                    reasons.append({'key': key, 'status': 'skipped', 'reason': 'already_applied'})
                    continue
                changed_playbook = _append_unique_block(self.root / APPROVED_PLAYBOOK_FILE, proposal['playbook_marker'], proposal['playbook_block'])
                changed_faq = _append_unique_block(self.root / FAQ_APPROVED_FILE, proposal['faq_marker'], proposal['faq_block'])
                if changed_playbook or changed_faq:
                    self.append_jsonl(LEARNING_REVIEW_FILE, {
                        'ts': time.time(),
                        'status': 'applied',
                        'key': key,
                        'fingerprint': fingerprint,
                        'pattern_count': len(records),
                        'contact_ids': sorted({str(record.get('contact_id') or '') for record in records if record.get('contact_id')}),
                        'changed_playbook': changed_playbook,
                        'changed_faq': changed_faq,
                        'playbook_marker': proposal['playbook_marker'],
                        'faq_marker': proposal['faq_marker'],
                        'summary': proposal['summary'],
                    })
                    applied_hashes.add(fingerprint)
                    applied += 1
                    reasons.append({'key': key, 'status': 'applied', 'reason': proposal['summary']})
                else:
                    skipped += 1
                    reasons.append({'key': key, 'status': 'skipped', 'reason': 'no_content_change'})
            return {'applied': applied, 'skipped': skipped, 'reasons': reasons}


def learning_root() -> Path:
    return Path(os.environ.get(LEARNING_ROOT_ENV) or DEFAULT_LEARNING_ROOT)


def ensure_aivo_learning_store(root: Path | None = None) -> AivoLearningStore:
    return AivoLearningStore(root=root)


def build_aivo_learning_prompt_context(*, contact_id: str, sender_id: str = '', chat_id: str = '', contact_name: str = '', conversation_mode: str = 'sales', product_id: str = '', recent_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return ensure_aivo_learning_store().build_prompt_context(contact_id=contact_id, sender_id=sender_id, chat_id=chat_id, contact_name=contact_name, conversation_mode=conversation_mode, product_id=product_id, recent_history=recent_history)


def record_aivo_whatsapp_turn(*, contact_id: str, sender_id: str = '', chat_id: str = '', contact_name: str = '', user_text: str = '', assistant_text: str = '', conversation_mode: str = 'sales', product_id: str = '') -> dict[str, Any]:
    return ensure_aivo_learning_store().record_turn(contact_id=contact_id, sender_id=sender_id, chat_id=chat_id, contact_name=contact_name, user_text=user_text, assistant_text=assistant_text, conversation_mode=conversation_mode, product_id=product_id)


def upsert_aivo_product(*, product_id: str, name: str, category: str = '', description: str = '', target_audience: str = '', use_case: str = '', market: str = '', price: str = '', features: list[str] | None = None, specs: list[str] | None = None, constraints: list[str] | None = None, notes: str = '', source_url: str = '', analyze: bool = True, analysis_override: dict[str, Any] | None = None) -> dict[str, Any]:
    return ensure_aivo_learning_store().upsert_product(
        product_id=product_id,
        name=name,
        category=category,
        description=description,
        target_audience=target_audience,
        use_case=use_case,
        market=market,
        price=price,
        features=features,
        specs=specs,
        constraints=constraints,
        notes=notes,
        source_url=source_url,
        analyze=analyze,
        analysis_override=analysis_override,
    )


def list_aivo_products() -> list[dict[str, Any]]:
    return ensure_aivo_learning_store().list_products()


def load_aivo_product(product_id: str) -> dict[str, Any]:
    return ensure_aivo_learning_store().load_product_profile(product_id)


def promote_aivo_learning_queue(*, min_hits: int = 2) -> dict[str, Any]:
    return ensure_aivo_learning_store().promote_learning_queue(min_hits=min_hits)


def default_playbook() -> str:
    return dedent("""
    # Playbook aprovado de vendas de insumos

    ## Objetivo
    Vender insumos industriais e materias-primas B2B com foco em especificacao tecnica, confiabilidade de fornecimento e aplicacao final.

    ## Regras aprovadas
    - Não tratar como chatbot, CRM ou automação de WhatsApp.
    - Não prometer mudanças automáticas em preço, garantia, política, laudo, pureza ou performance sem aprovação humana.
    - Nunca compartilhar senhas, chaves, tokens, arquivos sensíveis ou dados privados.
    - Se alguém pedir informação sensível, recusar de forma curta e segura.
    - Responder com linguagem curta, natural e consultiva.
    - Quando houver pergunta sobre preço, lote, embalagem, prazo ou condição comercial, responder diretamente com os dados aprovados.
    - Para remarketing, usar retomada curta, beneficio claro e oportunidade de compra.

    ## Itens aprovados
    Bicarbonato de sodio, bicarbonato de amonio, bicarbonato de potassio, sagu, fecula de batata, fecula de mandioca, polvilho doce e polvilho azedo.

    ## Dados a sempre confirmar
    Pureza, granulometria, umidade, pH, composicao, embalagem, prazo de validade, origem, laudo e aplicacao final.

    ## Tom aprovado
    - Direto quando o cliente estiver objetivo.
    - Mais vendedor quando houver abertura para avançar.
    - Sempre conduzir para um próximo passo claro.
    """).strip()


def default_faq() -> str:
    return dedent("""
    # FAQ aprovado

    ## Perguntas frequentes aprovadas

    ### O que vendemos?
    Insumos industriais e materias-primas como bicarbonatos, sagu, feculas e polvilhos.

    ### Quais dados sempre precisam entrar na venda?
    Pureza, granulometria, umidade, pH, embalagem, aplicacao final, prazo e origem.

    ### Como responder quando perguntarem preço ou condicao comercial?
    Usar diretamente os dados aprovados do produto cadastrado, sem inventar especificacao.

    ### Posso alterar preço, promessa ou política automaticamente?
    Não. Isso exige aprovação humana.

    ### Posso compartilhar senhas, chaves ou arquivos sensíveis?
    Não. Esse tipo de informação nunca deve ser compartilhada.
    """).strip()


def detect_intent(user_text: str, assistant_text: str = '', conversation_mode: str = 'sales') -> str:
    text = _normalize(f'{user_text} {assistant_text}')
    if conversation_mode == 'remarketing':
        return 'remarketing'
    if any(term in text for term in ('preco', 'preço', 'valor', 'plano', 'planos', 'assinatura', 'mensalidade', 'anuidade')):
        return 'pricing'
    if any(term in text for term in ('comprar', 'quero', 'checkout', 'assinar', 'fechar', 'seguir com a compra')):
        return 'purchase'
    if any(term in text for term in ('como funciona', 'o que e', 'o que é', 'demo', 'demonstra', 'video', 'vídeo', 'mostra')):
        return 'demo'
    if any(term in text for term in ('comparar', 'diferença', 'diferenca', 'melhor custo', 'starter', 'pro', 'max')):
        return 'comparison'
    if any(term in text for term in ('sim', 'ok', 'beleza', 'certo')):
        return 'follow_up'
    return 'other'


def detect_objection(user_text: str) -> str:
    text = _normalize(user_text)
    if any(term in text for term in ('caro', 'preço', 'preco', 'valor', 'dinheiro', 'custar')):
        return 'price'
    if any(term in text for term in ('app', 'celular', 'telefone', 'depend', 'baixar')):
        return 'app_dependency'
    if any(term in text for term in ('privacidade', 'privado', 'seguro', 'sigilo', 'dados')):
        return 'privacy'
    if any(term in text for term in ('confio', 'garantia', 'segurança', 'seguranca', 'funciona mesmo')):
        return 'trust'
    if any(term in text for term in ('comparar', 'melhor', 'outro', 'vs')):
        return 'comparison'
    if any(term in text for term in ('tempo', 'demora', 'prático', 'pratico', 'curva')):
        return 'time'
    if any(term in text for term in ('qualidade', 'transcrição', 'transcricao', 'áudio', 'audio')):
        return 'quality'
    return 'other'


def detect_next_step(user_text: str, assistant_text: str, *, intent: str, objection: str) -> str:
    if intent == 'pricing':
        return 'share_plans'
    if intent == 'purchase':
        return 'send_checkout'
    if intent == 'demo':
        return 'show_demo'
    if intent == 'remarketing':
        return 'offer_discount'
    if objection in {'price', 'privacy', 'trust', 'quality', 'app_dependency'}:
        return 'answer_objection'
    return 'ask_short_qualifier'


def summarize_turn(user_text: str, assistant_text: str, *, intent: str, objection: str, next_step: str) -> str:
    user = _shorten(user_text, 120)
    assistant = _shorten(assistant_text, 160)
    parts = [f'intento={intent}', f'objeção={objection}', f'próximo={next_step}']
    if user:
        parts.append(f'cliente="{user}"')
    if assistant:
        parts.append(f'resposta="{assistant}"')
    return '; '.join(parts)


def merge_summary(previous: str, current: str, *, max_chars: int = 420) -> str:
    previous = (previous or '').strip()
    current = (current or '').strip()
    if not previous:
        return _shorten(current, max_chars)
    merged = f'{previous} | {current}' if current and current not in previous else previous
    return _shorten(merged, max_chars)


def update_tags(existing: Any, *, intent: str, objection: str, next_step: str) -> list[str]:
    tags: list[str] = []
    for item in existing or []:
        if item and item not in tags:
            tags.append(str(item))
    for item in (intent, objection, next_step):
        if item and item != 'other' and item not in tags:
            tags.append(item)
    return tags[:12]


def build_learning_suggestion(*, contact_id: str, sender_id: str, chat_id: str, contact_name: str, intent: str, objection: str, next_step: str, user_text: str, assistant_text: str, conversation_mode: str) -> dict[str, Any] | None:
    needs_review = intent == 'other' or objection != 'other' or next_step == 'ask_short_qualifier'
    if not needs_review:
        return None
    return {
        'ts': time.time(),
        'status': 'pending_review',
        'contact_id': contact_id,
        'sender_id': sender_id,
        'chat_id': chat_id,
        'contact_name': contact_name,
        'conversation_mode': conversation_mode,
        'pattern_type': 'learning_suggestion',
        'intent': intent,
        'objection': objection,
        'next_step': next_step,
        'sample_user_text': _shorten(user_text, 240),
        'sample_assistant_text': _shorten(assistant_text, 240),
        'reason': 'pattern not yet approved or needs human review',
    }


def _normalize(text: str) -> str:
    text = unicodedata.normalize('NFKD', text or '')
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r'[^\w\s]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _shorten(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


def _contains_commercial_terms(text: str) -> bool:
    normalized = _normalize(text)
    return any(term in normalized for term in COMMERCIAL_LOCK_TERMS)


def _autopromote_learning_enabled() -> bool:
    return str(os.environ.get('AIVO_LEARNING_AUTOPROMOTE', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}


def _learning_promotion_key(record: dict[str, Any]) -> str | None:
    user_text = str(record.get('sample_user_text') or '')
    assistant_text = str(record.get('sample_assistant_text') or '')
    if _contains_commercial_terms(user_text) or _contains_commercial_terms(assistant_text):
        return None
    intent = str(record.get('intent') or 'other')
    objection = str(record.get('objection') or 'other')
    next_step = str(record.get('next_step') or 'other')
    if objection == 'price':
        return None
    if objection in SAFE_PROMOTABLE_OBJECTIONS:
        return f'objection:{objection}'
    if intent == 'other' and next_step == 'ask_short_qualifier':
        return 'clarify_short_question'
    return None


def _build_learning_promotion(key: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None

    sample_user = _shorten(str(records[0].get('sample_user_text') or ''), 160)
    sample_assistant = _shorten(str(records[0].get('sample_assistant_text') or ''), 200)

    if key == 'clarify_short_question':
        summary = 'Aprimorar respostas para mensagens vagas com acolhimento curto e uma pergunta objetiva.'
        playbook_marker = 'aivo-learning:clarify_short_question'
        faq_marker = 'aivo-learning:clarify_short_question'
        playbook_block = dedent("""
        <!-- aivo-learning:clarify_short_question -->
        ### Mensagem vaga
        - Reconhecer a intenção em uma frase curta.
        - Fazer uma única pergunta objetiva.
        - Conduzir para o próximo passo sem alongar.
        """).strip()
        faq_block = dedent("""
        <!-- aivo-learning:clarify_short_question -->
        ### Como responder quando o contato manda uma mensagem vaga?
        Responder curto: reconhecer, fazer uma pergunta objetiva e indicar o próximo passo.
        """).strip()
    elif key == 'objection:privacy':
        summary = 'Consolidar resposta curta para dúvidas de privacidade, sem prometer além do aprovado.'
        playbook_marker = 'aivo-learning:objection-privacy'
        faq_marker = 'aivo-learning:objection-privacy'
        playbook_block = dedent("""
        <!-- aivo-learning:objection-privacy -->
        ### Dúvida de privacidade
        - Validar a preocupação sem entrar em promessas novas.
        - Responder com firmeza curta e manter o foco no que já está aprovado.
        """).strip()
        faq_block = dedent("""
        <!-- aivo-learning:objection-privacy -->
        ### Como responder quando o cliente pergunta sobre privacidade?
        Validar a dúvida e responder só com o que já está aprovado, sem inventar garantias novas.
        """).strip()
    elif key == 'objection:trust':
        summary = 'Consolidar resposta curta para dúvidas de confiança e credibilidade.'
        playbook_marker = 'aivo-learning:objection-trust'
        faq_marker = 'aivo-learning:objection-trust'
        playbook_block = dedent("""
        <!-- aivo-learning:objection-trust -->
        ### Dúvida de confiança
        - Responder com segurança e sem exageros.
        - Reforçar o valor do produto com clareza e próxima ação.
        """).strip()
        faq_block = dedent("""
        <!-- aivo-learning:objection-trust -->
        ### Como responder quando o cliente quer mais confiança antes de avançar?
        Responder com segurança, sem exagerar, e conduzir para o próximo passo com clareza.
        """).strip()
    elif key == 'objection:app_dependency':
        summary = 'Ajustar explicação para dependência de aplicativo com linguagem objetiva.'
        playbook_marker = 'aivo-learning:objection-app-dependency'
        faq_marker = 'aivo-learning:objection-app-dependency'
        playbook_block = dedent("""
        <!-- aivo-learning:objection-app-dependency -->
        ### Dependência de app
        - Explicar o fluxo de uso de forma simples.
        - Evitar tecnicismo desnecessário.
        """).strip()
        faq_block = dedent("""
        <!-- aivo-learning:objection-app-dependency -->
        ### Como responder quando perguntam sobre dependência de aplicativo?
        Explicar o fluxo de uso em linguagem simples e prática, sem tecnicismo desnecessário.
        """).strip()
    elif key == 'objection:quality':
        summary = 'Consolidar resposta curta para dúvidas de qualidade e áudio.'
        playbook_marker = 'aivo-learning:objection-quality'
        faq_marker = 'aivo-learning:objection-quality'
        playbook_block = dedent("""
        <!-- aivo-learning:objection-quality -->
        ### Dúvida de qualidade
        - Explicar o que o produto resolve e o que depende do uso correto.
        - Não prometer perfeição absoluta.
        """).strip()
        faq_block = dedent("""
        <!-- aivo-learning:objection-quality -->
        ### Como responder quando o cliente questiona qualidade ou áudio?
        Explicar o que o produto resolve e lembrar que resultado também depende do uso correto.
        """).strip()
    else:
        return None

    fingerprint = hashlib.sha256(
        f'{key}|{summary}|{sample_user}|{sample_assistant}'.encode('utf-8')
    ).hexdigest()
    return {
        'fingerprint': fingerprint,
        'summary': summary,
        'playbook_marker': playbook_marker,
        'faq_marker': faq_marker,
        'playbook_block': playbook_block,
        'faq_block': faq_block,
    }


def _append_unique_block(path: Path, marker: str, block: str) -> bool:
    marker = marker.strip()
    block = block.strip()
    if not marker or not block:
        return False
    try:
        existing = path.read_text(encoding='utf-8')
    except OSError:
        existing = ''
    if marker in existing:
        return False
    updated = existing.rstrip()
    if updated:
        updated += '\n\n'
    updated += block + '\n'
    _write_text_atomic(path, updated)
    return True


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.stem}_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        from utils import atomic_replace
        atomic_replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
