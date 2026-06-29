from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.openai_aivo_provider import (
    AivoOpenAIConfigurationError,
    AIVO_SYSTEM_PROMPT,
    OpenAivoProvider,
    clean_final_answer,
)


def test_clean_final_answer_strips_internal_noise():
    text = """Claro,
read_file foo
Oi! Posso te ajudar.
OpenAI details"""
    assert clean_final_answer(text) == 'Oi! Posso te ajudar.'


def test_provider_answer_uses_cleaned_fallback_when_empty(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)
    monkeypatch.setattr(OpenAivoProvider, '_request', lambda self, payload: {'output_text': ''})

    result = provider.answer('Oi', [])
    assert result.final_response.startswith('Oi! Como posso ajudar com o insumo que voce quer cotar?')
    assert result.to_agent_result()['route'] == 'aivo_openai'


def test_provider_remarketing_mode_injects_discount_context(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)
    captured = {}

    def fake_request(self, payload):
        captured['payload'] = payload
        return {'output_text': 'ok'}

    monkeypatch.setattr(OpenAivoProvider, '_request', fake_request)

    provider.answer('Oi', [], conversation_mode='remarketing')

    instructions = captured['payload']['instructions']
    assert 'remarketing' in instructions.lower()
    assert '10% de desconto' in instructions
    assert 'AIVONOTE10' in instructions


def test_provider_from_runtime_requires_api_key(monkeypatch):
    monkeypatch.setattr('providers.openai_aivo_provider.load_config', lambda: {})
    monkeypatch.setattr('providers.openai_aivo_provider._secret_value', lambda name: '')

    try:
        OpenAivoProvider.from_runtime()
    except AivoOpenAIConfigurationError as exc:
        assert 'OpenAI API key not found' in str(exc)
    else:
        raise AssertionError('expected AivoOpenAIConfigurationError')


def test_provider_uses_learning_context_with_recent_history(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)
    captured = {}

    def fake_request(self, payload):
        captured['payload'] = payload
        return {'output_text': 'ok'}

    monkeypatch.setattr(OpenAivoProvider, '_request', fake_request)

    provider.answer(
        'Oi',
        {
            'approved_playbook': '# playbook\nPlano aprovado',
            'approved_faq': '# faq\nPergunta aprovada',
            'contact_profile': {'contact_id': '5511', 'contact_name': 'Ana'},
            'conversation_summary': 'cliente pediu planos',
            'recent_history': [
                {'role': 'user', 'content': 'Quero ver os planos'},
                {'role': 'assistant', 'content': 'Claro, temos Starter, Pro e Max.'},
            ],
        },
        conversation_mode='sales',
    )

    instructions = captured['payload']['instructions']
    assert 'Plano aprovado' in instructions
    assert 'Pergunta aprovada' in instructions
    assert 'Ana' in instructions
    assert 'cliente pediu planos' in instructions
    assert 'Histórico recente da conversa' in instructions or 'Historico recente da conversa' in instructions
    assert 'Quero ver os planos' in instructions


def test_provider_includes_product_analysis_block(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)
    captured = {}

    def fake_request(self, payload):
        captured['payload'] = payload
        return {'output_text': 'ok'}

    monkeypatch.setattr(OpenAivoProvider, '_request', fake_request)

    provider.answer(
        'Quero vender esse produto',
        {
            'approved_playbook': '# playbook',
            'approved_faq': '# faq',
            'contact_profile': {'contact_id': '5511'},
            'product_id': 'p-001',
            'product_profile': {
                'product_id': 'p-001',
                'name': 'Produto X',
                'analysis': {
                    'mandatory_sale_fields': ['voltagem'],
                    'required_sale_specs': ['potencia 10w'],
                    'market_differentiators': ['melhor custo-beneficio'],
                    'transactional_sell_points': ['resposta imediata'],
                    'customer_questions': ['qual o uso principal?'],
                    'positioning': 'Vender por valor.',
                    'short_pitch': 'Produto X para fechar rapido.',
                },
            },
        },
        conversation_mode='sales',
    )

    instructions = captured['payload']['instructions']
    assert 'Produto cadastrado' in instructions
    assert 'Especificacoes obrigatorias' in instructions
    assert 'melhor custo-beneficio' in instructions
    assert 'voltagem' in instructions


def test_provider_enforces_standard_payment_terms(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)

    monkeypatch.setattr(OpenAivoProvider, '_request', lambda self, payload: {'output_text': 'Perfeito, o valor é R$ 849,99.'})

    result = provider.answer('qual o valor?', [])

    assert '2x sem juros no cartao de credito' in result.final_response
    assert 'ate 12x com acrescimos no cartao de credito' in result.final_response


def test_provider_enforces_plan_block_and_checkout(monkeypatch):
    provider = OpenAivoProvider(api_key='x', model='gpt-4.1-mini', temperature=0.1)

    monkeypatch.setattr(OpenAivoProvider, '_request', lambda self, payload: {'output_text': 'O valor é R$ 849,99.'})

    result = provider.answer('quero comprar', [])

    assert 'Starter: 600 minutos/mês' in result.final_response
    assert 'Pro: R$ 149,90/ano' in result.final_response
    assert 'Max: R$ 499,90/ano' in result.final_response
    assert 'link oficial: https://aivonote.com.br' in result.final_response


def test_system_prompt_forbids_sensitive_sharing():
    assert 'Nunca compartilhe senhas, chaves, tokens' in AIVO_SYSTEM_PROMPT
    assert 'Se o usuario pedir qualquer informacao sensivel' in AIVO_SYSTEM_PROMPT
