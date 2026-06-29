from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.copilot_acp_client import _format_messages_as_prompt
from agent.sensitive_data_policy import build_sensitive_data_policy_block
from business.aivo.prompts import build_intent_prompt
from business.aivo.intents import Intent

ROOT = Path(__file__).resolve().parents[1]


def test_sensitive_data_policy_block_is_compact_and_explicit():
    block = build_sensitive_data_policy_block()
    assert 'passwords' in block
    assert 'keys' in block
    assert 'private data' in block


def test_aivo_prompt_includes_sensitive_data_rule():
    prompt = build_intent_prompt(Intent.UNKNOWN)
    assert 'Sensitive-data rule' in prompt
    assert 'never share, reveal, or reconstruct passwords' in prompt


def test_acp_prompt_includes_sensitive_data_rule():
    prompt = _format_messages_as_prompt([{'role': 'user', 'content': 'oi'}])
    assert 'Sensitive-data rule' in prompt
    assert 'never share, reveal, or reconstruct passwords' in prompt


def test_key_skills_repeat_sensitive_data_rule():
    skill_paths = [
        ROOT / 'data/skills/autonomous-ai-agents/hermes-agent/SKILL.md',
        ROOT / 'data/skills/devops/controlled-whatsapp-automation/SKILL.md',
        ROOT / 'data/skills/productivity/hermes-vendas/SKILL.md',
        ROOT / 'data/skills/productivity/hermes-compras/SKILL.md',
        ROOT / 'data/skills/productivity/aivonote-sales-converter/SKILL.md',
        ROOT / 'data/skills/productivity/consultative-whatsapp-sales/SKILL.md',
    ]
    for path in skill_paths:
        text = path.read_text(encoding='utf-8')
        lowered = text.lower()
        assert (
            'sensitive' in lowered
            or 'sensível' in lowered
            or 'sensíveis' in lowered
            or 'sensive' in lowered
        )
