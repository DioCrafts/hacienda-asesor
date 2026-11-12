"""LLM spend metering: recording, prices, limits and the metering wrapper (no network)."""

from collections.abc import Callable
import contextvars
from datetime import UTC, date, datetime
from decimal import Decimal
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.anthropic_provider import AnthropicProvider
from hacienda_gpt.agent.providers.base import ModelTurn, ProviderError, ToolCall, ToolResult, Usage
from hacienda_gpt.agent.providers.openai_provider import OpenAIProvider
from hacienda_gpt.api.routers import consumo as consumo_router
from hacienda_gpt.cli.consumo import cli
from hacienda_gpt.ops import consumo
from hacienda_gpt.ops.consumo import (
    ConsumoStore,
    MeteredProvider,
    Precio,
    Presupuesto,
    PresupuestoAgotadoError,
    TablaPrecios,
    calcular_coste,
    cargar_precios,
    contexto_consumo,
    hoy_madrid,
)

PRECIOS = TablaPrecios(
    consultado_el=date(2026, 1, 1),
    moneda="USD",
    unidad="por millón de tokens",
    nota="de prueba",
    fuente="de prueba",
    modelos={
        "modelo-a": Precio(
            input=Decimal("2"), output=Decimal("10"), cache_read=Decimal("0.5"), cache_write=Decimal("4")
        ),
        "modelo-sin-precio": Precio(),
        "modelo-sin-cache": Precio(input=Decimal("1"), output=Decimal("1")),
    },
)


class FakeConversation:
    def __init__(self, provider: "FakeProvider") -> None:
        self._provider = provider

    def step(self, *, allow_tools: bool = True, on_text: Callable[[str], None] | None = None) -> ModelTurn:
        self._provider.steps.append(allow_tools)
        if on_text is not None:
            on_text("hola")
        if self._provider.error is not None:
            raise self._provider.error
        return ModelTurn(text="hola", tool_calls=[], stop_reason="end", usage=dict(self._provider.turn_usage))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._provider.tool_results.extend(results)

    def add_user_message(self, text: str) -> None:
        self._provider.user_messages.append(text)


class FakeProvider:
    """A provider without an ``on_usage`` hook: usage only travels in ``ModelTurn.usage``."""

    name = "fake"

    def __init__(self, model: str = "modelo-a", turn_usage: dict[str, int] | None = None) -> None:
        self.model = model
        self.turn_usage = {"input_tokens": 1000, "output_tokens": 100} if turn_usage is None else turn_usage
        self.steps: list[bool] = []
        self.json_calls: list[dict[str, Any]] = []
        self.tool_results: list[ToolResult] = []
        self.user_messages: list[str] = []
        self.started: dict[str, Any] = {}
        self.error: Exception | None = None

    def start(self, *, system, tools, history, user_message) -> FakeConversation:
        self.started = {"system": system, "tools": tools, "history": history, "user_message": user_message}
        return FakeConversation(self)

    def complete_json(self, *, system, user, schema, schema_name, attachments=None) -> dict[str, Any]:
        self.json_calls.append({"system": system, "user": user, "schema_name": schema_name, "attachments": attachments})
        if self.error is not None:
            raise self.error
        return {"ok": True, "n": len(self.json_calls)}


@pytest.fixture
def store(tmp_path) -> ConsumoStore:
    return ConsumoStore(str(tmp_path / "state.sqlite3"), precios=PRECIOS)


def _rows(store: ConsumoStore) -> list[dict[str, Any]]:
    return [dict(row) for row in store._conn.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()]


def _usage(model: str = "modelo-a", **tokens: int) -> Usage:
    return Usage(provider="fake", model=model, purpose="agente", **tokens)


# --------------------------------------------------------------------------- #
# Prices and cost
# --------------------------------------------------------------------------- #


def test_cost_adds_every_token_type_at_its_own_price() -> None:
    usage = _usage(
        input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=2_000_000, cache_write_tokens=250_000
    )
    # 2 + 5 + 1 + 1
    assert calcular_coste(usage, PRECIOS) == Decimal("9")
    assert calcular_coste(_usage(input_tokens=1), PRECIOS) == Decimal("0.000002")


def test_unknown_model_or_missing_price_gives_no_cost() -> None:
    assert calcular_coste(_usage("modelo-desconocido", input_tokens=10), PRECIOS) is None
    assert calcular_coste(_usage("modelo-sin-precio", input_tokens=10), PRECIOS) is None
    # A price is only needed for the token types actually used.
    assert calcular_coste(_usage("modelo-sin-cache", input_tokens=1_000_000), PRECIOS) == Decimal("1")
    assert calcular_coste(_usage("modelo-sin-cache", input_tokens=10, cache_read_tokens=5), PRECIOS) is None
    # No usage data at all: the cost is unknown, not zero.
    assert calcular_coste(Usage(provider="fake", model="modelo-a", reported=False), PRECIOS) is None


def test_dated_snapshot_uses_the_base_model_price() -> None:
    assert calcular_coste(_usage("modelo-a-20260401", input_tokens=1_000_000), PRECIOS) == Decimal("2")
    assert calcular_coste(_usage("modelo-a-mini", input_tokens=1_000_000), PRECIOS) is None


def test_bundled_price_table_is_valid_and_covers_the_configured_models() -> None:
    tabla = cargar_precios(consumo.PRECIOS_PATH)
    assert tabla.moneda == "USD" and "cambian" in tabla.nota
    assert tabla.consultado_el <= date.today()
    # Every default model id of settings.py has an entry (possibly with null prices).
    assert tabla.precio_de("claude-opus-5-5") is not None
    assert tabla.precio_de("gpt-4o-mini") is not None
    openai = tabla.modelos["gpt-4o-mini"]
    assert (openai.input, openai.output, openai.cache_read, openai.cache_write) == (None, None, None, None)
    assert calcular_coste(Usage(provider="openai", model="gpt-4o-mini", input_tokens=100), tabla) is None
    claude = tabla.modelos["claude-opus-5-5"]
    assert claude.input is not None and claude.cache_write == claude.input * Decimal("1.25")


def test_price_table_can_be_replaced_from_settings(tmp_path, monkeypatch) -> None:
    path = tmp_path / "precios.json"
    path.write_text(
        json.dumps(
            {
                "consultado_el": "2026-02-02",
                "moneda": "USD",
                "unidad": "por millón de tokens",
                "nota": "n",
                "fuente": "f",
                "modelos": {"x": {"input": "3", "output": "6"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LLM_PRICES_PATH", str(path))
    assert cargar_precios().precio_de("x").input == Decimal("3")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_records_a_call_with_its_cost(store) -> None:
    coste = store.registrar(
        _usage(input_tokens=1000, output_tokens=200, cache_read_tokens=300, cache_write_tokens=50),
        user_id="u1",
        case_id="case_1",
    )
    assert coste == Decimal("0.00435")
    (row,) = _rows(store)
    assert row["user_id"] == "u1" and row["case_id"] == "case_1" and row["purpose"] == "agente"
    assert (row["provider"], row["model"]) == ("fake", "modelo-a")
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (300, 50)
    assert row["cost_usd"] == pytest.approx(0.00435)
    assert datetime.fromisoformat(row["ts"]).tzinfo is not None


def test_unknown_model_is_recorded_with_null_cost(store) -> None:
    assert store.registrar(_usage("modelo-desconocido", input_tokens=10)) is None
    assert store.registrar(Usage(provider="fake", model="modelo-a", purpose="x", reported=False)) is None
    desconocido, sin_datos = _rows(store)
    assert desconocido["cost_usd"] is None and desconocido["input_tokens"] == 10
    assert desconocido["user_id"] is None and desconocido["case_id"] is None
    assert sin_datos["cost_usd"] is None and sin_datos["input_tokens"] is None
    total = store.resumen(desde=hoy_madrid(), hasta=hoy_madrid()).total
    assert (total.llamadas, total.llamadas_sin_coste, total.coste_usd) == (2, 2, Decimal(0))


def test_aggregates_by_day_user_case_and_purpose(store) -> None:
    def add(ts: datetime, purpose: str, user: str | None, case: str | None, model: str = "modelo-a") -> None:
        usage = Usage(provider="fake", model=model, input_tokens=1_000_000, purpose=purpose)
        store.registrar(usage, user_id=user, case_id=case, ts=ts)

    # 23:30 UTC on 9 June is already 10 June in Madrid (UTC+2 in summer).
    add(datetime(2026, 6, 9, 23, 30, tzinfo=UTC), "agente", "u1", "c1")
    add(datetime(2026, 6, 10, 10, 0, tzinfo=UTC), "agente", "u1", "c2")
    add(datetime(2026, 6, 10, 11, 0, tzinfo=UTC), "verificador", "u2", None)
    add(datetime(2026, 6, 11, 9, 0, tzinfo=UTC), "documentos", None, "c1", model="modelo-desconocido")
    add(datetime(2026, 6, 9, 12, 0, tzinfo=UTC), "agente", "u1", "c1")  # the day before the range

    resumen = store.resumen(desde=date(2026, 6, 10), hasta=date(2026, 6, 11))

    assert (resumen.total.llamadas, resumen.total.llamadas_sin_coste) == (4, 1)
    assert resumen.total.coste_usd == Decimal("6") and resumen.total.input_tokens == 4_000_000
    assert [(a.clave, a.llamadas, a.coste_usd) for a in resumen.por_dia] == [
        ("2026-06-10", 3, Decimal("6")),
        ("2026-06-11", 1, Decimal("0")),
    ]
    assert [(a.clave, a.llamadas, a.coste_usd) for a in resumen.por_proposito] == [
        ("agente", 2, Decimal("4")),
        ("verificador", 1, Decimal("2")),
        ("documentos", 1, Decimal("0")),
    ]
    assert {a.clave: a.llamadas for a in resumen.por_usuario} == {"u1": 2, "u2": 1, None: 1}
    assert {a.clave: a.llamadas for a in resumen.por_caso} == {"c1": 2, "c2": 1, None: 1}
    assert [a.clave for a in store.agregar("usuario", desde=date(2026, 6, 9), hasta=date(2026, 6, 9))] == ["u1"]
    assert store.coste_del_dia(date(2026, 6, 10)) == Decimal("6")
    assert store.coste_del_dia(date(2026, 6, 10), user_id="u2") == Decimal("2")


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_no_limits_never_blocks(store) -> None:
    store.registrar(_usage(input_tokens=10**9), user_id="u1")
    Presupuesto(store).comprobar("u1", "c1", llamadas_en_pregunta=10**6)


def test_daily_limit_blocks_the_next_call_with_a_spanish_message(store) -> None:
    provider = FakeProvider(turn_usage={"input_tokens": 1_000_000, "output_tokens": 0})  # 2 USD per call
    metered = MeteredProvider(
        provider, store, purpose="agente", presupuesto=Presupuesto(store, diario_usd=Decimal("3"))
    )
    conversation = metered.start(system="S", tools=[], history=[], user_message="¿IVA?")

    conversation.step()  # 0 spent -> allowed, now 2
    conversation.step()  # 2 < 3 -> allowed, now 4
    with pytest.raises(PresupuestoAgotadoError) as excinfo:
        conversation.step()
    with pytest.raises(PresupuestoAgotadoError):
        metered.complete_json(system="S", user="U", schema={}, schema_name="x")

    error = excinfo.value
    assert isinstance(error, ProviderError) and not error.retryable
    assert error.limite == "diario"
    assert "límite diario de gasto" in str(error) and "Se restablece el " in str(error)
    assert "a las 00:00 (hora peninsular española)" in str(error)
    # The model was not called once the limit was reached, and nothing else was recorded.
    assert len(provider.steps) == 2 and provider.json_calls == []
    assert len(_rows(store)) == 2


def test_daily_limit_resets_at_madrid_midnight(store) -> None:
    presupuesto = Presupuesto(store, diario_usd=Decimal("1"))
    store.registrar(_usage(input_tokens=1_000_000), ts=datetime(2026, 3, 10, 22, 30, tzinfo=UTC))  # 23:30 in Madrid

    with pytest.raises(PresupuestoAgotadoError) as excinfo:
        presupuesto.comprobar(None, None, ahora=datetime(2026, 3, 10, 22, 45, tzinfo=UTC))
    assert excinfo.value.se_restablece == datetime(2026, 3, 10, 23, 0, tzinfo=UTC)
    assert "el 11/03/2026 a las 00:00" in str(excinfo.value)
    # 00:15 in Madrid the next day: a new budget.
    presupuesto.comprobar(None, None, ahora=datetime(2026, 3, 10, 23, 15, tzinfo=UTC))


def test_per_user_daily_limit_only_blocks_that_user(store) -> None:
    presupuesto = Presupuesto(store, diario_por_usuario_usd=Decimal("1"))
    store.registrar(_usage(input_tokens=1_000_000), user_id="u1")

    with pytest.raises(PresupuestoAgotadoError) as excinfo:
        presupuesto.comprobar("u1", None)
    assert excinfo.value.limite == "diario_por_usuario"
    assert "Has alcanzado tu límite diario" in str(excinfo.value)
    presupuesto.comprobar("u2", None)
    presupuesto.comprobar(None, None)  # unattributed calls have no per-user limit


def test_calls_without_a_known_cost_do_not_count_and_are_warned_about(store, caplog) -> None:
    provider = FakeProvider(model="modelo-desconocido")
    metered = MeteredProvider(
        provider, store, purpose="agente", presupuesto=Presupuesto(store, diario_usd=Decimal("1"))
    )
    with caplog.at_level(logging.WARNING, logger="hacienda_gpt.ops.consumo"):
        for _ in range(3):
            metered.complete_json(system="S", user="U", schema={}, schema_name="x")
    assert len(provider.json_calls) == 3
    assert sum("does not count towards the daily USD limits" in r.message for r in caplog.records) == 1


def test_per_question_call_cap(store) -> None:
    provider = FakeProvider()
    presupuesto = Presupuesto(store, max_llamadas_por_pregunta=3)
    metered = MeteredProvider(provider, store, user_id="u1", case_id="c1", purpose="agente", presupuesto=presupuesto)
    verificador = metered.con_proposito("verificador")  # same question, same counter
    conversation = metered.start(system="S", tools=[], history=[], user_message="¿IVA?")

    conversation.step()
    conversation.step()
    verificador.complete_json(system="S", user="U", schema={}, schema_name="x")
    with pytest.raises(PresupuestoAgotadoError) as excinfo:
        conversation.step()

    assert excinfo.value.limite == "llamadas_por_pregunta" and excinfo.value.se_restablece is None
    assert "máximo de 3 llamadas al modelo de IA por pregunta" in str(excinfo.value)
    assert [row["purpose"] for row in _rows(store)] == ["agente", "agente", "verificador"]
    assert {(row["user_id"], row["case_id"]) for row in _rows(store)} == {("u1", "c1")}

    metered.nueva_pregunta(user_id="u2")
    conversation.step()
    assert _rows(store)[-1]["user_id"] == "u2" and _rows(store)[-1]["case_id"] is None


def test_shared_wrapper_counts_per_question_through_the_context(store) -> None:
    provider = FakeProvider()
    presupuesto = Presupuesto(store, max_llamadas_por_pregunta=2)
    metered = MeteredProvider(provider, store, purpose="agente", presupuesto=presupuesto, compartido=True)

    def ask() -> None:
        metered.complete_json(system="S", user="U", schema={}, schema_name="x")

    for user in ("u1", "u2"):  # each question gets its own count
        with contexto_consumo(user_id=user, case_id=f"case_{user}") as contexto:
            ask()
            # A worker thread runs inside a copy of the context and shares the count.
            contextvars.copy_context().run(ask)
            with pytest.raises(PresupuestoAgotadoError):
                ask()
            assert contexto.llamadas == 2
    # Outside any question a long-lived wrapper has nothing to count against.
    for _ in range(5):
        ask()

    rows = _rows(store)
    assert [row["user_id"] for row in rows] == ["u1", "u1", "u2", "u2", None, None, None, None, None]
    assert rows[0]["case_id"] == "case_u1" and rows[-1]["case_id"] is None


def test_limits_from_settings(store, monkeypatch) -> None:
    for name in ("LLM_DAILY_BUDGET_USD", "LLM_DAILY_BUDGET_USD_PER_USER", "LLM_MAX_CALLS_PER_QUESTION"):
        monkeypatch.setattr(settings, name, "")
    vacio = Presupuesto.desde_settings(store)
    assert (vacio.diario_usd, vacio.diario_por_usuario_usd, vacio.max_llamadas_por_pregunta) == (None, None, None)
    assert not vacio.limita_en_usd

    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", " 12.50 ")
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD_PER_USER", "0.75")
    monkeypatch.setattr(settings, "LLM_MAX_CALLS_PER_QUESTION", "20")
    lleno = Presupuesto.desde_settings(store)
    assert (lleno.diario_usd, lleno.diario_por_usuario_usd, lleno.max_llamadas_por_pregunta) == (
        Decimal("12.50"),
        Decimal("0.75"),
        20,
    )

    for name, value in (
        ("LLM_DAILY_BUDGET_USD", "mucho"),
        ("LLM_DAILY_BUDGET_USD", "0"),
        ("LLM_DAILY_BUDGET_USD_PER_USER", "-1"),
        ("LLM_DAILY_BUDGET_USD", "NaN"),
        ("LLM_MAX_CALLS_PER_QUESTION", "0"),
        ("LLM_MAX_CALLS_PER_QUESTION", "2.5"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(settings, name, value)
            with pytest.raises(ValueError, match=name):
                Presupuesto.desde_settings(store)


# --------------------------------------------------------------------------- #
# Wrapper transparency
# --------------------------------------------------------------------------- #


def test_wrapper_returns_exactly_what_the_provider_returns(store) -> None:
    provider = FakeProvider()
    metered = MeteredProvider(provider, store, user_id="u1", case_id="c1", purpose="agente")
    assert (metered.name, metered.model) == ("fake", "modelo-a")
    assert metered.wrapped is provider and metered.steps is provider.steps  # other attributes pass through

    conversation = metered.start(system="S", tools=[], history=[{"role": "user", "content": "h"}], user_message="¿IVA?")
    assert provider.started == {
        "system": "S",
        "tools": [],
        "history": [{"role": "user", "content": "h"}],
        "user_message": "¿IVA?",
    }
    chunks: list[str] = []
    turn = conversation.step(allow_tools=False, on_text=chunks.append)
    assert turn == ModelTurn(
        text="hola", tool_calls=[], stop_reason="end", usage={"input_tokens": 1000, "output_tokens": 100}
    )
    assert chunks == ["hola"] and provider.steps == [False]

    conversation.add_tool_results([ToolResult(call_id="c", content="r")])
    conversation.add_user_message("otra")
    assert provider.tool_results == [ToolResult(call_id="c", content="r")] and provider.user_messages == ["otra"]

    assert metered.complete_json(system="S", user="U", schema={"a": 1}, schema_name="x") == {"ok": True, "n": 1}
    assert provider.json_calls[0] == {"system": "S", "user": "U", "schema_name": "x", "attachments": None}
    metered.complete_json(system="S", user="U", schema={}, schema_name="x", attachments=["adjunto"])
    assert provider.json_calls[1]["attachments"] == ["adjunto"]

    step_row, json_row, _ = _rows(store)
    assert (step_row["input_tokens"], step_row["output_tokens"]) == (1000, 100)
    assert step_row["cost_usd"] == pytest.approx(0.003)
    # complete_json returns no usage and this provider has no hook: recorded, size unknown.
    assert json_row["input_tokens"] is None and json_row["cost_usd"] is None


def test_wrapper_lets_provider_errors_through_and_survives_a_broken_store(store, caplog) -> None:
    provider = FakeProvider()
    metered = MeteredProvider(provider, store, purpose="agente")
    provider.error = ProviderError("caído", retryable=True)
    with pytest.raises(ProviderError, match="caído") as excinfo:
        metered.complete_json(system="S", user="U", schema={}, schema_name="x")
    assert excinfo.value is provider.error
    assert _rows(store) == []  # nothing was reported for the failed call

    provider.error = None
    store.close()
    with caplog.at_level(logging.WARNING, logger="hacienda_gpt.ops.consumo"):
        assert metered.complete_json(system="S", user="U", schema={}, schema_name="x") == {"ok": True, "n": 2}
    assert any("could not record LLM usage" in record.message for record in caplog.records)


def test_wrapper_works_with_the_scripted_agent_fakes(store) -> None:
    spec = importlib.util.spec_from_file_location(
        "agent_fakes", Path(__file__).resolve().parents[1] / "agent" / "fakes.py"
    )
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    scripted = fakes.ScriptedProvider(
        [fakes.tool_turn(("buscar_fuentes", {"consulta": "iva"})), fakes.text_turn("21 %")], [{"a": 1}]
    )
    metered = MeteredProvider(scripted, store, purpose="agente")

    conversation = metered.start(system="S", tools=[], history=[], user_message="¿IVA?")
    primero = conversation.step()
    assert primero.tool_calls == [ToolCall(id="call_0", name="buscar_fuentes", arguments={"consulta": "iva"})]
    assert conversation.step().text == "21 %"
    assert metered.complete_json(system="S", user="U", schema={}, schema_name="x") == {"a": 1}
    assert not hasattr(scripted, "on_usage")  # doubles are not modified
    assert [(row["model"], row["input_tokens"]) for row in _rows(store)] == [
        ("scripted-1", 10),
        ("scripted-1", 10),
        ("scripted-1", None),
    ]


# --------------------------------------------------------------------------- #
# Usage capture in the real providers (fake SDK clients)
# --------------------------------------------------------------------------- #


class _Queue:
    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    stream = create


def _anthropic_client(responses: list) -> SimpleNamespace:
    queue = _Queue(list(responses))
    return SimpleNamespace(messages=queue, beta=SimpleNamespace(messages=queue))


def _claude_response(text: str = "ok", **usage: Any) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        model="claude-opus-5-5",
        usage=SimpleNamespace(**usage),
    )


class _FakeStream:
    def __init__(self, chunks: list[str], final: Any) -> None:
        self._chunks, self._final = chunks, final

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    @property
    def text_stream(self):
        yield from self._chunks

    def get_final_message(self):
        return self._final


def test_anthropic_reports_usage_per_call_including_streams() -> None:
    client = _anthropic_client(
        [
            _claude_response(
                input_tokens=100, output_tokens=20, cache_read_input_tokens=80, cache_creation_input_tokens=40
            ),
            _FakeStream(["o", "k"], _claude_response(input_tokens=7, output_tokens=3)),
            _claude_response('{"a": 1}', input_tokens=5, output_tokens=2, cache_read_input_tokens=None),
        ]
    )
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)
    seen: list[Usage] = []
    provider.on_usage = seen.append

    conversation = provider.start(system="S", tools=[], history=[], user_message="¿IVA?")
    conversation.step()
    conversation.add_user_message("sigue")
    conversation.step(on_text=lambda chunk: None)
    assert provider.complete_json(system="S", user="U", schema={}, schema_name="x") == {"a": 1}

    assert seen == [
        Usage("anthropic", "claude-opus-5-5", 100, 20, 80, 40),
        Usage("anthropic", "claude-opus-5-5", 7, 3, 0, 0),
        Usage("anthropic", "claude-opus-5-5", 5, 2, 0, 0),
    ]


def test_anthropic_reports_the_model_that_served_the_request() -> None:
    response = _claude_response(input_tokens=1, output_tokens=1)
    response.model = "claude-otro"  # e.g. after a refusal fallback
    provider = AnthropicProvider(model="claude-opus-5-5", client=_anthropic_client([response]))
    seen: list[Usage] = []
    provider.on_usage = seen.append
    provider.start(system="S", tools=[], history=[], user_message="x").step()
    assert seen[0].model == "claude-otro"


def test_missing_or_broken_usage_never_raises() -> None:
    sin_usage = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn")
    raro = _claude_response(input_tokens="muchos", output_tokens=-4, cache_read_input_tokens=True)
    provider = AnthropicProvider(model="claude-opus-5-5", client=_anthropic_client([sin_usage, raro, raro, raro]))

    # No hook installed: nothing to do.
    provider.start(system="S", tools=[], history=[], user_message="x")
    seen: list[Usage] = []
    provider.on_usage = seen.append
    conversation = provider.start(system="S", tools=[], history=[], user_message="x")
    # The response has no ``usage`` attribute at all (step() itself reads it defensively too).
    sin_usage.usage = None
    assert conversation.step().text == "ok"
    assert seen == []
    assert conversation.step().text == "ok"
    assert seen == [Usage("anthropic", "claude-opus-5-5", 0, 0, 0, 0)]

    def explode(usage: Usage) -> None:
        raise RuntimeError("callback roto")

    provider.on_usage = explode
    assert conversation.step().text == "ok"


def _openai_response(content: str = "ok", usage: Any = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage)


def _openai_client(responses: list) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=_Queue(list(responses))))


def _chunk(content: str | None = None, finish_reason: str | None = None, usage: Any = None) -> SimpleNamespace:
    if content is None and finish_reason is None:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=usage)


def test_openai_reports_usage_per_call_including_streams() -> None:
    cached = SimpleNamespace(
        prompt_tokens=100, completion_tokens=9, prompt_tokens_details=SimpleNamespace(cached_tokens=60)
    )
    client = _openai_client(
        [
            _openai_response(usage=SimpleNamespace(prompt_tokens=50, completion_tokens=7)),
            iter(
                [
                    _chunk("o"),
                    _chunk("k", finish_reason="stop"),
                    _chunk(usage=SimpleNamespace(prompt_tokens=30, completion_tokens=4)),
                ]
            ),
            _openai_response('{"a": 1}', usage=cached),
            _openai_response("sin datos", usage=None),  # local servers often send no usage
        ]
    )
    provider = OpenAIProvider(model="gpt-4o-mini", client=client)
    seen: list[Usage] = []
    provider.on_usage = seen.append

    conversation = provider.start(system="S", tools=[], history=[], user_message="¿IVA?")
    conversation.step()
    conversation.add_user_message("sigue")
    conversation.step(on_text=lambda chunk: None)
    assert provider.complete_json(system="S", user="U", schema={}, schema_name="x") == {"a": 1}
    conversation.add_user_message("más")
    assert conversation.step().text == "sin datos"

    assert seen == [
        Usage("openai", "gpt-4o-mini", 50, 7),
        Usage("openai", "gpt-4o-mini", 30, 4),
        # Cached prompt tokens are split out of the input, not counted twice.
        Usage("openai", "gpt-4o-mini", 40, 9, 60),
    ]


def test_openai_stream_with_running_totals_is_reported_once() -> None:
    client = _openai_client(
        [
            iter(
                [
                    _chunk("o", usage=SimpleNamespace(prompt_tokens=30, completion_tokens=1)),
                    _chunk("k", finish_reason="stop", usage=SimpleNamespace(prompt_tokens=30, completion_tokens=2)),
                ]
            )
        ]
    )
    provider = OpenAIProvider(model="qwen", client=client, name="local")
    seen: list[Usage] = []
    provider.on_usage = seen.append
    provider.start(system="S", tools=[], history=[], user_message="x").step(on_text=lambda chunk: None)
    assert seen == [Usage("local", "qwen", 30, 2)]


def test_metered_real_provider_records_every_api_call_with_its_cost(tmp_path) -> None:
    store = ConsumoStore(str(tmp_path / "state.sqlite3"), precios=cargar_precios(consumo.PRECIOS_PATH))
    client = _anthropic_client(
        [
            _claude_response(
                input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000, cache_creation_input_tokens=400
            ),
            _claude_response('{"a": 1}', input_tokens=10, output_tokens=5),
        ]
    )
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)
    agente = MeteredProvider(provider, store, user_id="u1", case_id="c1", purpose="agente")
    verificador = MeteredProvider(provider, store, purpose="verificador")  # two wrappers, one provider

    agente.start(system="S", tools=[], history=[], user_message="¿IVA?").step()
    verificador.complete_json(system="S", user="U", schema={}, schema_name="x")
    # A direct, unmetered use of the provider is not recorded and does not fail.
    client.messages.responses.append(_claude_response(input_tokens=1, output_tokens=1))
    provider.start(system="S", tools=[], history=[], user_message="x").step()

    primero, segundo = _rows(store)
    assert (primero["purpose"], primero["user_id"], primero["case_id"]) == ("agente", "u1", "c1")
    assert (primero["input_tokens"], primero["cache_read_tokens"], primero["cache_write_tokens"]) == (1000, 3000, 400)
    tabla = cargar_precios(consumo.PRECIOS_PATH).modelos["claude-opus-5-5"]
    esperado = (1000 * tabla.input + 200 * tabla.output + 3000 * tabla.cache_read + 400 * tabla.cache_write) / 10**6
    assert primero["cost_usd"] == pytest.approx(float(esperado))
    assert (segundo["purpose"], segundo["user_id"]) == ("verificador", None)


# --------------------------------------------------------------------------- #
# Router and CLI
# --------------------------------------------------------------------------- #


def _client(store: ConsumoStore) -> TestClient:
    app = FastAPI()
    app.include_router(consumo_router.router)
    app.dependency_overrides[consumo_router.get_consumo_store] = lambda: store
    return TestClient(app)


def test_usage_endpoint_reports_today_and_30_days_by_purpose(store, monkeypatch) -> None:
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD", "5")
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET_USD_PER_USER", "")
    monkeypatch.setattr(settings, "LLM_MAX_CALLS_PER_QUESTION", "")
    ahora = datetime.now(UTC)
    store.registrar(_usage(input_tokens=1_000_000), ts=ahora)
    store.registrar(Usage("fake", "modelo-a", 500_000, purpose="verificador"), ts=ahora)
    store.registrar(_usage(input_tokens=1_000_000), ts=datetime(2020, 1, 1, tzinfo=UTC))  # out of range
    hoy = hoy_madrid(ahora)

    response = _client(store).get("/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["hoy"]["desde"] == body["hoy"]["hasta"] == hoy.isoformat()
    assert body["hoy"]["total"]["llamadas"] == 2 and Decimal(body["hoy"]["total"]["coste_usd"]) == Decimal("3")
    assert [(a["clave"], a["llamadas"]) for a in body["hoy"]["por_proposito"]] == [("agente", 1), ("verificador", 1)]
    assert body["ultimos_30_dias"]["total"]["llamadas"] == 2
    assert (hoy - date.fromisoformat(body["ultimos_30_dias"]["desde"])).days == 29
    assert Decimal(body["limites"]["diario_usd"]) == Decimal("5")
    assert body["limites"]["max_llamadas_por_pregunta"] is None
    assert body["precios_consultados_el"] == "2026-01-01"


def test_usage_endpoint_requires_the_api_key_when_configured(store, monkeypatch) -> None:
    monkeypatch.setenv("HACIENDA_API_KEY", "secreto")
    client = _client(store)
    assert client.get("/usage").status_code == 401
    assert client.get("/usage", headers={"X-API-Key": "secreto"}).status_code == 200


def test_cli_resumen_prints_json(tmp_path) -> None:
    db = tmp_path / "state.sqlite3"
    escritor = ConsumoStore(str(db), precios=PRECIOS)
    escritor.registrar(_usage(input_tokens=1_000_000), user_id="u1", case_id="c1")
    escritor.close()

    result = CliRunner().invoke(cli, ["resumen", "--dias", "7", "--db", str(db)])

    assert result.exit_code == 0, result.output
    salida = json.loads(result.output)
    assert salida["total"]["llamadas"] == 1
    assert [a["clave"] for a in salida["por_usuario"]] == ["u1"]
    assert [a["clave"] for a in salida["por_caso"]] == ["c1"]
    assert [a["clave"] for a in salida["por_dia"]] == [hoy_madrid().isoformat()]
    assert (date.fromisoformat(salida["hasta"]) - date.fromisoformat(salida["desde"])).days == 6
    assert "precios_consultados_el" in salida
    assert CliRunner().invoke(cli, ["resumen", "--dias", "0", "--db", str(db)]).exit_code != 0


def test_a_hook_set_by_someone_else_is_kept_and_usage_is_captured_once(store) -> None:
    client = _anthropic_client([_claude_response(input_tokens=10, output_tokens=5)])
    provider = AnthropicProvider(model="claude-opus-5-5", client=client)
    seen: list[Usage] = []
    provider.on_usage = seen.append
    primero = MeteredProvider(provider, store, purpose="agente")
    MeteredProvider(provider, store, purpose="verificador")

    primero.start(system="S", tools=[], history=[], user_message="x").step()

    assert len(seen) == 1 and len(_rows(store)) == 1
