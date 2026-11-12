"""LLM providers for the agent, selected by ``LLM_PROVIDER``."""

from __future__ import annotations

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.base import (
    Conversation,
    LLMProvider,
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "Conversation",
    "LLMProvider",
    "ModelTurn",
    "ProviderError",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "create_provider",
]


def create_provider(name: str | None = None, *, model: str | None = None) -> LLMProvider:
    """Build the provider named by ``name`` (default: ``settings.LLM_PROVIDER``).

    * ``anthropic`` — Claude via the official SDK (default).
    * ``openai``    — OpenAI Chat Completions.
    * ``local``     — any OpenAI-compatible server at ``LOCAL_LLM_BASE_URL``.
    """
    choice = (name or settings.LLM_PROVIDER).strip().lower()
    if choice in ("anthropic", "claude"):
        from hacienda_gpt.agent.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=model or settings.ANTHROPIC_MODEL,
            effort=settings.ANTHROPIC_EFFORT,
            refusal_fallback=settings.ANTHROPIC_REFUSAL_FALLBACK,
        )
    if choice == "openai":
        from hacienda_gpt.agent.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(model=model or settings.OPENAI_AGENT_MODEL)
    if choice == "local":
        from hacienda_gpt.agent.providers.openai_provider import OpenAIProvider

        if not (model or settings.LOCAL_LLM_MODEL):
            raise ValueError("LLM_PROVIDER=local requiere LOCAL_LLM_MODEL (p. ej. 'qwen3:14b').")
        return OpenAIProvider(
            model=model or settings.LOCAL_LLM_MODEL,
            base_url=settings.LOCAL_LLM_BASE_URL,
            # Local servers ignore the key but the SDK refuses to start without one.
            api_key=settings.LOCAL_LLM_API_KEY,
            name="local",
            timeout=settings.LOCAL_LLM_TIMEOUT,
        )
    raise ValueError(f"LLM_PROVIDER desconocido: {choice!r} (usa anthropic, openai o local)")
