from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aivo_learning import (
    build_aivo_learning_prompt_context,
    list_aivo_products,
    promote_aivo_learning_queue,
    record_aivo_whatsapp_turn,
    upsert_aivo_product,
)


@pytest.fixture()
def learning_root(tmp_path, monkeypatch):
    root = tmp_path / 'aivo-learning'
    monkeypatch.setenv('AIVO_LEARNING_ROOT', str(root))
    monkeypatch.setenv('AIVO_LEARNING_AUTOPROMOTE', '0')
    return root


def test_record_turn_updates_files_and_queue(learning_root):
    result = record_aivo_whatsapp_turn(
        contact_id='5511999999999',
        sender_id='5511999999999',
        chat_id='5511999999999@s.whatsapp.net',
        contact_name='Ana',
        user_text='Achei caro',
        assistant_text='Posso te mostrar o melhor custo-beneficio',
        conversation_mode='sales',
    )

    contacts = (learning_root / 'contacts.jsonl').read_text(encoding='utf-8').strip().splitlines()
    conversations = (learning_root / 'conversations.jsonl').read_text(encoding='utf-8').strip().splitlines()
    objections = (learning_root / 'objections.jsonl').read_text(encoding='utf-8').strip().splitlines()
    queue = (learning_root / 'learning_queue.jsonl').read_text(encoding='utf-8').strip().splitlines()

    assert contacts
    assert conversations
    assert objections
    assert queue
    contact = json.loads(contacts[-1])
    queued = json.loads(queue[-1])
    assert contact['contact_id'] == '5511999999999'
    assert 'price' in contact['tags']
    assert contact['last_objection'] == 'price'
    assert contact['summary']
    assert queued['status'] == 'pending_review'
    assert result['next_step'] in {'answer_objection', 'show_demo'}


def test_build_prompt_context_uses_approved_assets_and_summary(learning_root):
    record_aivo_whatsapp_turn(
        contact_id='5511999999999',
        sender_id='5511999999999',
        chat_id='5511999999999@s.whatsapp.net',
        contact_name='Ana',
        user_text='Quero planos',
        assistant_text='Starter, Pro e Max',
        conversation_mode='sales',
    )

    ctx = build_aivo_learning_prompt_context(
        contact_id='5511999999999',
        sender_id='5511999999999',
        chat_id='5511999999999@s.whatsapp.net',
        contact_name='Ana',
        conversation_mode='sales',
    )

    assert 'Itens aprovados' in ctx['approved_playbook']
    assert 'FAQ aprovado' in ctx['approved_faq']
    assert ctx['contact_profile']['contact_name'] == 'Ana'
    assert ctx['conversation_summary']


def test_learning_promotion_upgrades_safe_repeated_pattern(learning_root):
    record_aivo_whatsapp_turn(
        contact_id='5511999999001',
        sender_id='5511999999001',
        chat_id='5511999999001@s.whatsapp.net',
        contact_name='Bruno',
        user_text='me explica melhor',
        assistant_text='Claro, vou ser direto',
        conversation_mode='sales',
    )
    record_aivo_whatsapp_turn(
        contact_id='5511999999002',
        sender_id='5511999999002',
        chat_id='5511999999002@s.whatsapp.net',
        contact_name='Carla',
        user_text='me explica melhor',
        assistant_text='Claro, vou ser direto',
        conversation_mode='sales',
    )

    result = promote_aivo_learning_queue(min_hits=2)
    assert result['applied'] >= 1

    playbook = (learning_root / 'approved_playbook.md').read_text(encoding='utf-8')
    faq = (learning_root / 'faq_approved.md').read_text(encoding='utf-8')
    reviews = (learning_root / 'learning_reviews.jsonl').read_text(encoding='utf-8').strip().splitlines()

    assert 'Mensagem vaga' in playbook
    assert 'Como responder quando o contato manda uma mensagem vaga?' in faq
    assert len(reviews) == 1
    assert json.loads(reviews[0])['status'] == 'applied'


def test_learning_promotion_skips_price_patterns(learning_root):
    record_aivo_whatsapp_turn(
        contact_id='5511999999003',
        sender_id='5511999999003',
        chat_id='5511999999003@s.whatsapp.net',
        contact_name='Diego',
        user_text='Achei caro',
        assistant_text='Vou te mostrar o melhor custo-beneficio',
        conversation_mode='sales',
    )
    record_aivo_whatsapp_turn(
        contact_id='5511999999004',
        sender_id='5511999999004',
        chat_id='5511999999004@s.whatsapp.net',
        contact_name='Eva',
        user_text='Achei caro',
        assistant_text='Vou te mostrar o melhor custo-beneficio',
        conversation_mode='sales',
    )

    result = promote_aivo_learning_queue(min_hits=2)
    assert result['applied'] == 0

    reviews_path = learning_root / 'learning_reviews.jsonl'
    reviews = reviews_path.read_text(encoding='utf-8').strip().splitlines() if reviews_path.exists() else []
    assert reviews == []


def test_upsert_product_persists_analysis_and_prompt_context(learning_root, monkeypatch):
    class DummyAnalysis:
        def to_dict(self):
            return {
                'product_id': 'p-001',
                'product_name': 'Produto X',
                'required_sale_specs': ['potencia 10w', 'garantia 12 meses'],
                'mandatory_sale_fields': ['voltagem'],
                'market_differentiators': ['melhor custo-beneficio'],
                'transactional_sell_points': ['resposta imediata'],
                'customer_questions': ['qual o uso principal?'],
                'avoid_promises': ['nao prometer estoque infinito'],
                'positioning': 'Vender por valor e velocidade.',
                'short_pitch': 'Produto X para fechar rapido.',
                'confidence': 'high',
                'model': 'gpt-4.1-mini',
                'raw_summary': '{"ok": true}',
            }

    monkeypatch.setattr('aivo_learning.analyze_product_for_sales', lambda product: DummyAnalysis())

    record = upsert_aivo_product(
        product_id='p-001',
        name='Produto X',
        category='Acessorios',
        description='Produto de teste',
        target_audience='lojistas',
        use_case='venda transacional',
        market='Brasil',
        price='199,90',
        features=['agilidade', 'confianca'],
        specs=['voltagem'],
        constraints=['estoque limitado'],
        notes='nota interna',
    )

    products = list_aivo_products()
    ctx = build_aivo_learning_prompt_context(
        contact_id='5511999999999',
        sender_id='5511999999999',
        chat_id='5511999999999@s.whatsapp.net',
        contact_name='Ana',
        conversation_mode='sales',
        product_id='p-001',
    )

    assert products[-1]['product_id'] == 'p-001'
    assert record['analysis']['mandatory_sale_fields'] == ['voltagem']
    assert ctx['product_profile']['analysis']['required_sale_specs'][0] == 'potencia 10w'
    assert ctx['product_id'] == 'p-001'
    assert 'Bicarbonato de sodio' in (learning_root / 'approved_playbook.md').read_text(encoding='utf-8')
