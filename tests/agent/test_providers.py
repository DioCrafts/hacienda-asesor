"""Wire-format tests for the providers, against fake SDK clients (no network)."""

import json
from types import SimpleNamespace

import pytest

from hacienda_gpt.agent.providers import create_provider
from hacienda_gpt.agent.providers.anthropic_provider import AnthropicProvider
from hacienda_gpt.agent.providers.base import ProviderError, ToolResult, ToolSpec, normalize_history
from hacienda_gpt.agent.providers.openai_provider import OpenAIProvider

TOOLS = [ToolSpec(name="buscar_fuentes", description="busca", input_schema={"type": "object", "properties": {}})]


def test_normalize_history_drops_noise_and_leading_assistant_turns() -> None:
    history = [
        {"role": "assistant", "content": "hola"},
        {"role": "user", "content": "uno"},
        {"role": "system", "content": "ignórame"},
        {"role": "assistant", "content": ""},
        "basura",
        {"role": "assistant", "content": "dos"},
    ]
    assert normalize_history(history) == [{"role": "user", "content": "uno"}, {"role": "assistant", "content": "dos"}]


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


class _FakeMessages:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # Snapshot the message list: the conversation keeps appending to it.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list) -> None:
        self.messages = _FakeMessages(responses)
        self.beta = SimpleNamespace(messages=_FakeMessages(responses))
        # Both surfaces share one response queue.
        self.beta.messages.responses = self.messages.responses


def _claude_response(content: list, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80),
    )


def test_anthropic_tool_loop_request_shape_and_replay() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    tool_use = SimpleNamespace(type="tool_use", id="toolu_1", name="buscar_fuentes", input={"consulta": "iva"})
    client = _FakeAnthropic(
        [
            _claude_response([thinking, tool_use], "tool_use"),
            _claude_response([SimpleNamespace(type="text", text=" El tipo general es el 21 % [F1]. ")]),
        ]
    )
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)
    conversation = provider.start(
        system="SYS", tools=TOOLS, history=[{"role": "assistant", "content": "x"}], user_message="¿IVA?"
    )

    first = conversation.step()
    assert first.stop_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("toolu_1", "buscar_fuentes", {"consulta": "iva"})
    ]
    assert first.usage == {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80}

    conversation.add_tool_results(
        [ToolResult(call_id="toolu_1", content="[F1] …"), ToolResult(call_id="toolu_2", content="falló", is_error=True)]
    )
    second = conversation.step(allow_tools=False)
    assert second.stop_reason == "end" and second.text == "El tipo general es el 21 % [F1]."

    # Opus 5.5 → beta surface with the server-side refusal fallback.
    assert client.messages.calls == []
    call_1, call_2 = client.beta.messages.calls
    assert call_1["model"] == "claude-opus-5-5"
    assert call_1["betas"] == ["server-side-fallback-2026-07-01"] and call_1["fallbacks"] == "default"
    assert call_1["thinking"] == {"type": "adaptive"}
    assert call_1["output_config"] == {"effort": "high"}
    assert call_1["cache_control"] == {"type": "ephemeral"}
    assert call_1["system"] == "SYS"
    assert call_1["tools"] == [
        {"name": "buscar_fuentes", "description": "busca", "input_schema": TOOLS[0].input_schema}
    ]
    assert call_1["tool_choice"] == {"type": "auto"}
    assert call_1["messages"] == [{"role": "user", "content": "¿IVA?"}]  # leading assistant turn dropped
    assert "temperature" not in call_1 and "budget_tokens" not in json.dumps(call_1["thinking"])

    assert call_2["tool_choice"] == {"type": "none"}
    # The assistant turn is replayed verbatim (thinking block included) …
    assert call_2["messages"][1] == {"role": "assistant", "content": [thinking, tool_use]}
    # … and every tool result goes back in ONE user message.
    assert call_2["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "[F1] …"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "falló", "is_error": True},
        ],
    }


def test_anthropic_models_without_fallback_support_use_the_plain_endpoint() -> None:
    client = _FakeAnthropic([_claude_response([SimpleNamespace(type="text", text="ok")])])
    provider = AnthropicProvider(model="claude-haiku-5-5", client=client, effort="low")
    provider.start(system="S", tools=[], history=[], user_message="hola").step()

    (call,) = client.messages.calls
    assert "fallbacks" not in call and "betas" not in call and "tools" not in call
    assert call["output_config"] == {"effort": "low"}


def test_anthropic_complete_json_uses_structured_output_and_handles_refusal() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    client = _FakeAnthropic(
        [
            _claude_response([SimpleNamespace(type="text", text='{"tipo_respuesta": "respuesta"}')]),
            _claude_response([], "refusal"),
            _claude_response([SimpleNamespace(type="text", text="no es json")]),
        ]
    )
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)

    assert provider.complete_json(system="S", user="U", schema=schema, schema_name="x") == {
        "tipo_respuesta": "respuesta"
    }
    call = client.beta.messages.calls[0]
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": schema}}

    with pytest.raises(ProviderError):
        provider.complete_json(system="S", user="U", schema=schema, schema_name="x")
    with pytest.raises(ProviderError):
        provider.complete_json(system="S", user="U", schema=schema, schema_name="x")


def test_anthropic_refusal_stop_reason_is_surfaced() -> None:
    client = _FakeAnthropic([_claude_response([], "refusal")])
    turn = (
        AnthropicProvider(model="claude-opus-5-5", client=client)
        .start(system="S", tools=TOOLS, history=[], user_message="x")
        .step()
    )
    assert turn.stop_reason == "refusal" and turn.tool_calls == []


# --------------------------------------------------------------------------- #
# OpenAI / local (OpenAI-compatible)
# --------------------------------------------------------------------------- #


class _FakeCompletions:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _fake_openai(responses: list):
    completions = _FakeCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _chat_response(content: str | None = None, tool_calls: list | None = None, finish_reason: str = "stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=7),
    )


def _native_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_openai_tool_loop_request_shape_and_bad_arguments() -> None:
    client, completions = _fake_openai(
        [
            _chat_response(
                tool_calls=[
                    _native_call("c1", "buscar_fuentes", '{"consulta": "iva"}'),
                    _native_call("c2", "buscar_fuentes", "{consulta: roto"),
                ],
                finish_reason="tool_calls",
            ),
            _chat_response("El tipo general es el 21 % [F1]."),
        ]
    )
    provider = OpenAIProvider(model="gpt-test", client=client)
    conversation = provider.start(system="SYS", tools=TOOLS, history=[], user_message="¿IVA?")

    first = conversation.step()
    assert first.stop_reason == "tool_use"
    assert first.tool_calls[0].arguments == {"consulta": "iva"} and first.tool_calls[0].arguments_error is None
    assert first.tool_calls[1].arguments == {} and "JSON" in first.tool_calls[1].arguments_error
    assert first.usage == {"input_tokens": 50, "output_tokens": 7}

    conversation.add_tool_results(
        [ToolResult(call_id="c1", content="[F1] …"), ToolResult(call_id="c2", content="mal", is_error=True)]
    )
    second = conversation.step(allow_tools=False)
    assert second.stop_reason == "end" and second.text == "El tipo general es el 21 % [F1]."

    call_1, call_2 = completions.calls
    assert call_1["model"] == "gpt-test"
    assert call_1["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "¿IVA?"}]
    assert call_1["tools"][0] == {
        "type": "function",
        "function": {"name": "buscar_fuentes", "description": "busca", "parameters": TOOLS[0].input_schema},
    }
    assert call_1["tool_choice"] == "auto" and call_2["tool_choice"] == "none"
    assistant, tool_1, tool_2 = call_2["messages"][2:]
    assert assistant["role"] == "assistant" and [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]
    assert assistant["tool_calls"][1]["function"]["arguments"] == "{consulta: roto"  # replayed as sent
    assert tool_1 == {"role": "tool", "tool_call_id": "c1", "content": "[F1] …"}
    assert tool_2 == {"role": "tool", "tool_call_id": "c2", "content": "ERROR: mal"}


def test_openai_finish_reasons_map_to_stop_reasons() -> None:
    client, _ = _fake_openai(
        [_chat_response("a medias", finish_reason="length"), _chat_response(None, finish_reason="content_filter")]
    )
    conversation = OpenAIProvider(model="m", client=client).start(system="S", tools=[], history=[], user_message="x")
    assert conversation.step().stop_reason == "max_tokens"
    assert conversation.step().stop_reason == "refusal"


def test_openai_complete_json_falls_back_when_json_schema_is_unsupported() -> None:
    import httpx
    import openai

    bad_request = openai.BadRequestError(
        "response_format json_schema not supported",
        response=httpx.Response(400, request=httpx.Request("POST", "http://local/v1/chat/completions")),
        body=None,
    )
    client, completions = _fake_openai(
        [bad_request, _chat_response('Claro:\n```json\n{"tipo_respuesta": "abstencion", "afirmaciones": []}\n```')]
    )
    provider = OpenAIProvider(model="qwen3", client=client, name="local")
    schema = {"type": "object"}

    data = provider.complete_json(system="S", user="U", schema=schema, schema_name="informe")

    assert data == {"tipo_respuesta": "abstencion", "afirmaciones": []}
    strict_call, fallback_call = completions.calls
    assert strict_call["response_format"]["json_schema"] == {"name": "informe", "schema": schema, "strict": True}
    assert "response_format" not in fallback_call
    assert "JSON Schema" in fallback_call["messages"][0]["content"]


def test_openai_complete_json_rejects_non_json() -> None:
    client, _ = _fake_openai([_chat_response("no puedo")])
    with pytest.raises(ProviderError):
        OpenAIProvider(model="m", client=client).complete_json(system="S", user="U", schema={}, schema_name="x")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def test_create_provider_selects_backend(monkeypatch) -> None:
    from hacienda_gpt import settings

    claude = create_provider("anthropic")
    assert (claude.name, claude.model) == ("anthropic", "claude-opus-5-5")

    assert create_provider("openai", model="gpt-x").name == "openai"

    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", "")
    with pytest.raises(ValueError, match="LOCAL_LLM_MODEL"):
        create_provider("local")
    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", "qwen3:14b")
    local = create_provider("local")
    assert (local.name, local.model, local._base_url) == ("local", "qwen3:14b", settings.LOCAL_LLM_BASE_URL)

    with pytest.raises(ValueError, match="desconocido"):
        create_provider("gemini")


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


class _FakeStream:
    def __init__(self, chunks: list[str], final, error: Exception | None = None) -> None:
        self._chunks, self._final, self._error = chunks, final, error

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    @property
    def text_stream(self):
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def get_final_message(self):
        return self._final


class _FakeStreamingMessages(_FakeMessages):
    def stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def _streaming_anthropic(streams: list) -> tuple[SimpleNamespace, _FakeStreamingMessages]:
    beta = _FakeStreamingMessages(streams)
    return SimpleNamespace(messages=_FakeStreamingMessages([]), beta=SimpleNamespace(messages=beta)), beta


def test_anthropic_streaming_forwards_text_and_marks_tools_eager() -> None:
    final = _claude_response([SimpleNamespace(type="text", text="El tipo general es el 21 % [F1].")])
    client, beta = _streaming_anthropic([_FakeStream(["El tipo general ", "es el 21 % [F1]."], final)])
    chunks: list[str] = []

    turn = (
        AnthropicProvider(model="claude-opus-5-5", client=client)
        .start(system="S", tools=TOOLS, history=[], user_message="¿IVA?")
        .step(on_text=chunks.append)
    )

    assert chunks == ["El tipo general ", "es el 21 % [F1]."]
    assert turn.text == "El tipo general es el 21 % [F1]." and turn.stop_reason == "end"
    (call,) = beta.calls
    assert call["max_tokens"] == 64000  # streamed requests are not bound by the HTTP timeout
    assert call["fallbacks"] == "default" and call["thinking"] == {"type": "adaptive"}
    assert call["tools"][0]["eager_input_streaming"] is True


def test_anthropic_non_streamed_tools_do_not_carry_the_eager_flag() -> None:
    client = _FakeAnthropic([_claude_response([SimpleNamespace(type="text", text="ok")])])
    AnthropicProvider(model="claude-opus-5-5", client=client).start(
        system="S", tools=TOOLS, history=[], user_message="x"
    ).step()
    assert "eager_input_streaming" not in client.beta.messages.calls[0]["tools"][0]
    assert client.beta.messages.calls[0]["max_tokens"] == 16000


def test_anthropic_unparseable_streamed_tool_input_is_a_retryable_provider_error() -> None:
    client, _ = _streaming_anthropic([_FakeStream(["Voy a buscar"], None, error=ValueError("bad partial json"))])
    conversation = AnthropicProvider(model="claude-opus-5-5", client=client).start(
        system="S", tools=TOOLS, history=[], user_message="x"
    )
    with pytest.raises(ProviderError) as excinfo:
        conversation.step(on_text=lambda chunk: None)
    assert excinfo.value.retryable


def _chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    if content is None and tool_calls is None and finish_reason is None:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None)


def _call_fragment(index: int, call_id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_openai_streaming_forwards_text_and_reassembles_tool_calls() -> None:
    chunks_in = [
        _chunk(content="Voy a "),
        _chunk(content="buscarlo."),
        _chunk(tool_calls=[_call_fragment(0, "c1", "buscar_fuentes", '{"consul')]),
        _chunk(
            tool_calls=[_call_fragment(0, arguments='ta": "iva"}'), _call_fragment(1, "c2", "buscar_fuentes", "{}")]
        ),
        _chunk(finish_reason="tool_calls"),
        _chunk(usage=SimpleNamespace(prompt_tokens=40, completion_tokens=9)),
    ]
    client, completions = _fake_openai([iter(chunks_in)])
    chunks: list[str] = []
    conversation = OpenAIProvider(model="gpt-test", client=client).start(
        system="S", tools=TOOLS, history=[], user_message="¿IVA?"
    )

    turn = conversation.step(on_text=chunks.append)

    assert chunks == ["Voy a ", "buscarlo."]
    assert turn.stop_reason == "tool_use" and turn.text == "Voy a buscarlo."
    assert [(c.id, c.arguments) for c in turn.tool_calls] == [("c1", {"consulta": "iva"}), ("c2", {})]
    assert turn.usage == {"input_tokens": 40, "output_tokens": 9}
    (call,) = completions.calls
    assert call["stream"] is True and call["stream_options"] == {"include_usage": True}
    # The reassembled assistant turn is what gets replayed on the next request.
    assert conversation._messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"consulta": "iva"}'


def test_local_streaming_omits_stream_options_and_truncation_wins_over_tool_calls() -> None:
    chunks_in = [
        _chunk(tool_calls=[_call_fragment(0, "c1", "buscar_fuentes", '{"consulta": "i')]),
        _chunk(finish_reason="length"),
    ]
    client, completions = _fake_openai([iter(chunks_in)])
    turn = (
        OpenAIProvider(model="qwen3", client=client, name="local")
        .start(system="S", tools=TOOLS, history=[], user_message="x")
        .step(on_text=lambda chunk: None)
    )
    assert "stream_options" not in completions.calls[0]
    # A turn cut off mid tool call must surface as truncated, not as runnable tools.
    assert turn.stop_reason == "max_tokens" and turn.tool_calls[0].arguments_error is not None
