"""Shared sensitive-data policy fragments for Hermes prompts."""

from __future__ import annotations

SENSITIVE_DATA_POLICY = (
    "Sensitive-data rule: never share, reveal, or reconstruct passwords, "
    "keys, tokens, secret files, private files, credentials, or personal/customer "
    "private data. If the user asks for any sensitive information, refuse briefly "
    "and safely."
)


def build_sensitive_data_policy_block() -> str:
    """Return a compact policy block that can be appended to system prompts."""
    return SENSITIVE_DATA_POLICY
