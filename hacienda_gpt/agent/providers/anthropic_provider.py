"""Claude provider (default) — official ``anthropic`` SDK, Messages API."""

from __future__ import annotations

import base64
from collections.abc import Sequence
import json
from typing import Any

from hacienda_gpt.agent.providers.base import (
    Attachment,
    ModelTurn,
    ProviderError,
    StopReason,
    TextCallback,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    UsageCallback,
    normalize_history,
    report_usage,
    token_count,
)

# Non-streaming requests: 16k keeps a full answer + thinking comfortably under
# the SDK's HTTP timeout. Streamed requests have no such ceiling, so they get
# room to finish a long answer instead of being cut off mid-thought.
_MAX_TOKENS = 16000
_MAX_TOKENS_STREAMING = 64000

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


def usage_from_response(response: Any, *, provider: str, model: str) -> Usage | None:
    """Billed tokens of a Messages response; None when it carries no usage.

    The model is the one that served the request (``response.model``), which
    differs from the configured one after a refusal fallback.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    served = getattr(response, "model", None)
    return Usage(
        provider=provider,
        model=served if isinstance(served, str) and served else model,
        input_tokens=token_count(usage, "input_tokens"),
        output_tokens=token_count(usage, "output_tokens"),
        cache_read_tokens=token_count(usage, "cache_read_input_tokens"),
        cache_write_tokens=token_count(usage, "cache_creation_input_tokens"),
    )


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
        # Called with the token usage of every API call (see ops/consumo.py).
        self.on_usage: UsageCallback | None = None

    def _report(self, response: Any) -> Any:
        report_usage(self, lambda: usage_from_response(response, provider=self.name, model=self.model))
        return response

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic

            # Zero-arg: resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
            # `ant auth login` profile, in that order.
            self._client = anthropic.Anthropic()
        return self._client

    def _params(self, kwargs: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
        """Request parameters with adaptive thinking, effort and caching set."""
        output_config = {"effort": self.effort, **kwargs.pop("output_config", {})}
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            # Caches the growing prefix (tools → system → messages) so each
            # iteration of the tool loop re-reads the previous ones at ~0.1×.
            "cache_control": {"type": "ephemeral"},
            **kwargs,
        }

    def _create(self, **kwargs: Any) -> Any:
        """One non-streamed Messages call."""
        import anthropic

        client = self._ensure_client()
        params = self._params(kwargs, max_tokens=_MAX_TOKENS)
        try:
            if self._refusal_fallback:
                return self._report(client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **params))
            return self._report(client.messages.create(**params))
        except anthropic.APIError as exc:
            raise _wrap_errors(exc) from exc

    def _stream(self, on_text: TextCallback, **kwargs: Any) -> Any:
        """One streamed Messages call: forwards text chunks, returns the final message."""
        import anthropic

        client = self._ensure_client()
        params = self._params(kwargs, max_tokens=_MAX_TOKENS_STREAMING)
        try:
            if self._refusal_fallback:
                manager = client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **params)
            else:
                manager = client.messages.stream(**params)
            with manager as stream:
                for text in stream.text_stream:
                    on_text(text)
                # The final message accumulates the usage of the whole stream.
                return self._report(stream.get_final_message())
        except anthropic.APIError as exc:
            raise _wrap_errors(exc) from exc
        except ValueError as exc:
            # With eager input streaming the API no longer validates tool input;
            # the SDK raises ValueError from the iterator for JSON it cannot
            # parse at all. Nothing was appended to the history, so the caller
            # may simply retry the step.
            raise ProviderError(f"Claude emitió una llamada a herramienta ilegible ({exc})", retryable=True) from exc

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

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        del schema_name  # Claude's json_schema format carries no name
        content: str | list[dict[str, Any]] = user
        if attachments:
            # Claude reads PDFs (text and page images) and images natively; files go before the text.
            content = [
                {
                    "type": "document" if item.media_type == "application/pdf" else "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.media_type,
                        "data": base64.standard_b64encode(item.data).decode("ascii"),
                    },
                }
                for item in attachments
            ]
            content.append({"type": "text", "text": user})
        response = self._create(
            system=system,
            messages=[{"role": "user", "content": content}],
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

    def step(self, *, allow_tools: bool = True, on_text: TextCallback | None = None) -> ModelTurn:
        kwargs: dict[str, Any] = {"system": self._system, "messages": self._messages}
        if self._tools:
            # The tool list never changes between steps (that would invalidate
            # the prompt cache); "none" is how the loop asks for a final answer.
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = {"type": "auto" if allow_tools else "none"}
        if on_text is None:
            response = self._provider._create(**kwargs)
        else:
            if self._tools:
                # Streamed requests let tool inputs stream as generated. The API
                # then stops validating them, so the Toolbox type-checks every
                # argument and the agent never runs tools from a turn truncated
                # by max_tokens. (A run is streamed or not for its whole length,
                # so the tool list — and the prompt cache — stays stable.)
                kwargs["tools"] = [{**tool, "eager_input_streaming": True} for tool in self._tools]
            response = self._provider._stream(on_text, **kwargs)

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
