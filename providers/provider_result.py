from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProviderResult:
    """Structured result returned by a provider-backed message turn."""

    final_response: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    api_calls: int = 1
    completed: bool = True
    failed: bool = False
    partial: bool = False
    interrupted: bool = False
    provider: str = "openai"
    route: str = "aivo_openai"
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_agent_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "final_response": self.final_response,
            "messages": self.messages,
            "api_calls": self.api_calls,
            "completed": self.completed,
            "failed": self.failed,
            "partial": self.partial,
            "interrupted": self.interrupted,
        }
        if self.error:
            result["error"] = self.error
        if self.provider:
            result["provider"] = self.provider
        if self.route:
            result["route"] = self.route
        if self.metadata:
            result.update(self.metadata)
        return result
