"""OpenAI provider — also serves local models behind an OpenAI-compatible API.

``OpenAIProvider(base_url=...)`` is how the agent runs on Ollama, LM Studio,
``mlx_lm.server``, vLLM or llama.cpp's server: they all speak Chat Completions
with ``tools``. Tool-calling quality on small local models varies a lot; the
loop tolerates malformed arguments (reported back to the model as an error) but
cannot make a weak model plan well.
"""

from __future__ import annotations

import json
import re
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

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _wrap_errors(exc: Exception, label: str) -> ProviderError:
    import openai

    if isinstance(exc, openai.RateLimitError):
        return ProviderError(f"{label}: límite de peticiones alcanzado ({exc})", retryable=True)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError(f"{label}: credenciales no válidas o sin permiso ({exc})")
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(f"{label}: modelo o endpoint no encontrado ({exc})")
    if isinstance(exc, openai.APIStatusError):
        return ProviderError(f"{label}: error {exc.status_code} ({exc})", retryable=exc.status_code >= 500)
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(f"{label}: error de red ({exc})", retryable=True)
    return ProviderError(f"{label}: {exc}")


def _usage_dict(usage: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        value = getattr(usage, source, None)
        if isinstance(value, int):
            out[target] = value
    return out


class OpenAIProvider:
    def __init__(
        self,
        *,
        model: str,
        client: Any | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        name: str = "openai",
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.model = model
        self._client = client
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 2}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = OpenAI(**kwargs)
        return self._client

    def _create(self, **kwargs: Any) -> Any:
        import openai

        try:
            return self._ensure_client().chat.completions.create(model=self.model, **kwargs)
        except openai.APIError as exc:
            raise _wrap_errors(exc, self.name) from exc

    def start(
        self,
        *,
        system: str,
        tools: list[ToolSpec],
        history: list[dict[str, str]],
        user_message: str,
    ) -> OpenAIConversation:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *normalize_history(history),
            {"role": "user", "content": user_message},
        ]
        return OpenAIConversation(self, tools=tools, messages=messages)

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
        import openai

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        client = self._ensure_client()
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
            )
        except openai.BadRequestError:
            # Local servers frequently lack json_schema support. Degrade to a
            # prompt-carried schema; the caller validates the object anyway.
            fallback_messages = [
                {
                    "role": "system",
                    "content": (
                        f"{system}\n\nResponde ÚNICAMENTE con un objeto JSON válido que cumpla este JSON Schema, "
                        f"sin texto adicional:\n{json.dumps(schema, ensure_ascii=False)}"
                    ),
                },
                messages[1],
            ]
            response = self._create(messages=fallback_messages)
        except openai.APIError as exc:
            raise _wrap_errors(exc, self.name) from exc

        content = response.choices[0].message.content or ""
        return _parse_json_object(content, self.name)


def _parse_json_object(content: str, label: str) -> dict[str, Any]:
    candidates = [content]
    match = _JSON_OBJECT_RE.search(content)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ProviderError(f"{label} devolvió JSON no válido: {content[:200]}")


class OpenAIConversation:
    def __init__(self, provider: OpenAIProvider, *, tools: list[ToolSpec], messages: list[dict[str, Any]]) -> None:
        self._provider = provider
        self._tools = [
            {
                "type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema},
            }
            for tool in tools
        ]
        self._messages = messages

    def step(self, *, allow_tools: bool = True) -> ModelTurn:
        kwargs: dict[str, Any] = {"messages": self._messages}
        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        response = self._provider._create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        native_calls: list[dict[str, Any]] = []
        for call in message.tool_calls or []:
            raw_arguments = call.function.arguments or "{}"
            native_calls.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": raw_arguments},
                }
            )
            arguments: dict[str, Any] = {}
            error: str | None = None
            try:
                parsed = json.loads(raw_arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    error = "los argumentos deben ser un objeto JSON"
            except json.JSONDecodeError as exc:
                error = f"argumentos JSON no válidos: {exc}"
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments, arguments_error=error))

        assistant: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if native_calls:
            assistant["tool_calls"] = native_calls
        self._messages.append(assistant)

        stop_reason: StopReason
        if tool_calls:
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"
        elif choice.finish_reason == "content_filter":
            stop_reason = "refusal"
        else:
            stop_reason = "end"

        return ModelTurn(
            text=(message.content or "").strip(),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=_usage_dict(getattr(response, "usage", None)),
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for result in results:
            content = f"ERROR: {result.content}" if result.is_error else result.content
            self._messages.append({"role": "tool", "tool_call_id": result.call_id, "content": content})

    def add_user_message(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
