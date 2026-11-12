"""Claude provider (default) — official ``anthropic`` SDK, Messages API."""

from __future__ import annotations

import json
from typing import Any

from hacienda_gpt.agent.providers.base import (
    ModelTurn,
    ProviderError,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    normalize_history,
)

# Non-streaming requests: 16k keeps a full answer + thinking comfortably under
# the SDK's HTTP timeout. Tool inputs here are tiny (a query and a few filters).
_MAX_TOKENS = 16000

# Models whose safety classifiers can decline a request with
# ``stop_reason="refusal"``; for these we opt into the server-side fallback so a
# false positive on a tax question is re-run on another model inside the same
# call instead of surfacing as a dead end.
_FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _usage_dict(usage: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


def _wrap_errors(exc: Exception) -> ProviderError:
    import anthropic

    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError(f"Claude: límite de peticiones alcanzado ({exc})", retryable=True)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderError(f"Claude: credenciales no válidas o sin permiso ({exc})")
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(f"Claude: modelo o endpoint no encontrado ({exc})")
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderError(f"Claude: error {exc.status_code} ({exc})", retryable=exc.status_code >= 500)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(f"Claude: error de red ({exc})", retryable=True)
    return ProviderError(f"Claude: {exc}")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        effort: str = "high",
        client: Any | None = None,
        refusal_fallback: bool = True,
    ) -> None:
        self.model = model
        self.effort = effort
        self._client = client
        self._refusal_fallback = refusal_fallback and model.startswith(_FALLBACK_MODEL_PREFIXES)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            # Zero-arg: resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
            # `ant auth login` profile, in that order.
            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, **kwargs: Any) -> Any:
        """One Messages call, with adaptive thinking, effort and caching set."""
        import anthropic

        client = self._ensure_client()
        output_config = {"effort": self.effort, **kwargs.pop("output_config", {})}
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            # Caches the growing prefix (tools → system → messages) so each
            # iteration of the tool loop re-reads the previous ones at ~0.1×.
            "cache_control": {"type": "ephemeral"},
            **kwargs,
        }
        try:
            if self._refusal_fallback:
                return client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **params)
            return client.messages.create(**params)
        except anthropic.APIError as exc:
            raise _wrap_errors(exc) from exc

    def start(
        self,
        *,
        system: str,
        tools: list[ToolSpec],
        history: list[dict[str, str]],
        user_message: str,
    ) -> AnthropicConversation:
        messages: list[dict[str, Any]] = [*normalize_history(history), {"role": "user", "content": user_message}]
        return AnthropicConversation(self, system=system, tools=tools, messages=messages)

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
        del schema_name  # Claude's json_schema format carries no name
        response = self._create(
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason == "refusal":
            raise ProviderError("Claude declinó la petición de verificación.")
        if response.stop_reason == "max_tokens":
            raise ProviderError("Claude: la salida estructurada se truncó por max_tokens.")
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Claude devolvió JSON no válido: {text[:200]}") from exc
        if not isinstance(data, dict):
            raise ProviderError("Claude devolvió JSON que no es un objeto.")
        return data


class AnthropicConversation:
    def __init__(
        self,
        provider: AnthropicProvider,
        *,
        system: str,
        tools: list[ToolSpec],
        messages: list[dict[str, Any]],
    ) -> None:
        self._provider = provider
        self._system = system
        self._tools = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in tools
        ]
        self._messages = messages

    def step(self, *, allow_tools: bool = True) -> ModelTurn:
        kwargs: dict[str, Any] = {"system": self._system, "messages": self._messages}
        if self._tools:
            # The tool list never changes between steps (that would invalidate
            # the prompt cache); "none" is how the loop asks for a final answer.
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = {"type": "auto" if allow_tools else "none"}
        response = self._provider._create(**kwargs)

        # Replay the full content (thinking blocks included) on the next call.
        self._messages.append({"role": "assistant", "content": response.content})

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        return ModelTurn(
            text="".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop_reason=_STOP_REASONS.get(response.stop_reason or "end_turn", "end"),
            usage=_usage_dict(response.usage),
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # All results of a turn go back in ONE user message; splitting them
        # teaches the model to stop issuing parallel tool calls.
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.content,
                        **({"is_error": True} if result.is_error else {}),
                    }
                    for result in results
                ],
            }
        )

    def add_user_message(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
