"""OpenAI provider — also serves local models behind an OpenAI-compatible API.

``OpenAIProvider(base_url=...)`` is how the agent runs on Ollama, LM Studio,
``mlx_lm.server``, vLLM or llama.cpp's server: they all speak Chat Completions
with ``tools``. Tool-calling quality on small local models varies a lot; the
loop tolerates malformed arguments (reported back to the model as an error) but
cannot make a weak model plan well.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
import json
import re
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
    rasterizar_pdf,
    report_usage,
    token_count,
)

# Pages of a PDF sent as images in one request.
_MAX_PDF_PAGES = 20
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


def usage_from_response(response: Any, *, provider: str, model: str) -> Usage | None:
    """Billed tokens of a Chat Completions response (or of the last chunk of a stream).

    None when there is no usage object, as with many local servers.
    ``prompt_tokens`` includes the cached part of the prompt; it is split out
    when the API details it, so the two are never counted twice.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    prompt = token_count(usage, "prompt_tokens")
    cached = min(token_count(getattr(usage, "prompt_tokens_details", None), "cached_tokens"), prompt)
    return Usage(
        provider=provider,
        model=model,
        input_tokens=prompt - cached,
        output_tokens=token_count(usage, "completion_tokens"),
        cache_read_tokens=cached,
    )


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
        # Called with the token usage of every API call (see ops/consumo.py).
        self.on_usage: UsageCallback | None = None

    def _report(self, response: Any) -> None:
        """Report the usage carried by a response or by a stream chunk."""
        report_usage(self, lambda: usage_from_response(response, provider=self.name, model=self.model))

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
            response = self._ensure_client().chat.completions.create(model=self.model, **kwargs)
        except openai.APIError as exc:
            raise _wrap_errors(exc, self.name) from exc
        if not kwargs.get("stream"):
            # A streamed call returns an iterator; its usage arrives in a chunk (see _stream).
            self._report(response)
        return response

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

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        import openai

        content: str | list[dict[str, Any]] = user
        if attachments:
            # Images are the common denominator of OpenAI-compatible vision
            # models (local servers rarely accept PDF parts): PDFs go page by page.
            images: list[tuple[str, bytes]] = []
            for item in attachments:
                if item.media_type == "application/pdf":
                    images += [("image/png", page) for page in rasterizar_pdf(item.data, max_pages=_MAX_PDF_PAGES)]
                else:
                    images.append((item.media_type, item.data))
            content = [{"type": "text", "text": user}]
            content += [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{base64.standard_b64encode(data).decode('ascii')}"},
                }
                for media_type, data in images
            ]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
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
            self._report(response)
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

    def step(self, *, allow_tools: bool = True, on_text: TextCallback | None = None) -> ModelTurn:
        kwargs: dict[str, Any] = {"messages": self._messages}
        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto" if allow_tools else "none"

        if on_text is None:
            response = self._provider._create(**kwargs)
            choice = response.choices[0]
            content = choice.message.content or ""
            raw_calls = [
                (call.id, call.function.name, call.function.arguments or "{}")
                for call in choice.message.tool_calls or []
            ]
            finish_reason = choice.finish_reason
            usage = _usage_dict(getattr(response, "usage", None))
        else:
            content, raw_calls, finish_reason, usage = self._stream(on_text, kwargs)

        tool_calls: list[ToolCall] = []
        native_calls: list[dict[str, Any]] = []
        for call_id, name, raw_arguments in raw_calls:
            native_calls.append(
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw_arguments}}
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
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments, arguments_error=error))

        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if native_calls:
            assistant["tool_calls"] = native_calls
        self._messages.append(assistant)

        stop_reason: StopReason
        if finish_reason == "length":
            # Checked first: a truncated turn may carry half-written tool calls
            # that must not be executed.
            stop_reason = "max_tokens"
        elif tool_calls:
            stop_reason = "tool_use"
        elif finish_reason == "content_filter":
            stop_reason = "refusal"
        else:
            stop_reason = "end"

        return ModelTurn(text=content.strip(), tool_calls=tool_calls, stop_reason=stop_reason, usage=usage)

    def _stream(
        self, on_text: TextCallback, kwargs: dict[str, Any]
    ) -> tuple[str, list[tuple[str, str, str]], str | None, dict[str, int]]:
        """Streamed Chat Completions call: forwards text, reassembles tool calls."""
        import openai

        if self._provider.name == "openai":
            # Local OpenAI-compatible servers often reject stream_options, so the
            # token usage of streamed turns is only requested from OpenAI itself.
            kwargs = {**kwargs, "stream_options": {"include_usage": True}}
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        usage_chunk: Any = None
        try:
            for chunk in self._provider._create(stream=True, **kwargs):
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = _usage_dict(chunk_usage)
                    usage_chunk = chunk
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    text_parts.append(delta.content)
                    on_text(delta.content)
                for fragment in getattr(delta, "tool_calls", None) or []:
                    slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                    if fragment.id:
                        slot["id"] = fragment.id
                    function = getattr(fragment, "function", None)
                    if function is not None:
                        if function.name:
                            slot["name"] += function.name
                        if function.arguments:
                            slot["arguments"] += function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except openai.APIError as exc:  # raised mid-stream, after _create returned the iterator
            raise _wrap_errors(exc, self._provider.name) from exc
        if usage_chunk is not None:
            # Reported once, from the last chunk that carried it: OpenAI sends the
            # totals in a final chunk, some servers repeat running totals in each.
            self._provider._report(usage_chunk)

        raw_calls = [
            (slot["id"] or f"call_{index}", slot["name"], slot["arguments"] or "{}")
            for index, slot in sorted(calls.items())
        ]
        return "".join(text_parts), raw_calls, finish_reason, usage

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for result in results:
            content = f"ERROR: {result.content}" if result.is_error else result.content
            self._messages.append({"role": "tool", "tool_call_id": result.call_id, "content": content})

    def add_user_message(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})
