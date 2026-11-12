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
