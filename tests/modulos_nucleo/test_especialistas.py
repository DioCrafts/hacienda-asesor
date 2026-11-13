"""The central agent routes household questions to module specialists (scripted provider, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from hacienda_gpt.agent.agent import AgentMode, TaxAgent, TeamAgent
from hacienda_gpt.agent.prompts import AGENT_SYSTEM_PROMPT, CASE_TOOLS_ADDENDUM
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.modulos.fiscal import MODULO as FISCAL
from tests.agent.fakes import FakeSearcher, ScriptedProvider, corpus, text_turn, tool_turn, verdict
from .fakes import Llamadas, ModuloFalso, agua, instalar, seguros


class RecordingProvider(ScriptedProvider):
    """Keeps every conversation start (the advisor's and each specialist's)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.starts: list[dict] = []

    def start(self, **kwargs):
        self.starts.append(kwargs)
        return super().start(**kwargs)


def _case(**facts) -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="case-hogar",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        created_at=now,
        updated_at=now,
        facts=[
            Fact(
                fact_id=f"fact_{k}",
                name=k,
                value=v,
                value_type=FactValueType.NUMBER if isinstance(v, float) else FactValueType.STRING,
                source="t",
                confidence=1.0,
            )
            for k, v in facts.items()
        ],
    )


@pytest.fixture
def hogar():
    llamadas = Llamadas()
    modulos = (agua(llamadas), seguros(llamadas))
    instalar(*modulos)
    return llamadas, [FISCAL, *modulos]


def _names(tools) -> set[str]:
    return {tool.name for tool in tools}


def test_a_household_question_goes_to_the_specialist_and_comes_back_cited(hogar) -> None:
    llamadas, modulos = hogar
    provider = RecordingProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "¿Cuánto gasto de agua al mes?"})),
            tool_turn(("leer_expediente", {}), ("agua_consumo", {"x": 1})),
            text_turn("Consumo de 9,5 m3 y 31,20 € al mes [F1]."),
            text_turn("Tu agua te cuesta 31,20 € al mes [F1]."),
        ],
        [verdict("respuesta", ("Coste del agua", ["F1"], "respaldada"))],
    )
    case = _case(comunidad_autonoma="Madrid", seguros_poliza="P-9", agua_personas=3.0)

    result = TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run(
        "¿Cuánto me cuesta el agua?", case=case, modulos=modulos
    )

    assert result.mode is AgentMode.VERIFIED and result.answer == "Tu agua te cuesta 31,20 € al mes [F1]."
    assert result.citations[0].authority == "CALCULADORA" and result.citations[0].locator.startswith("modulo://agua/")
    principal, especialista = provider.starts
    # The advisor keeps every tax tool and gains one consult tool per non-tax module.
    assert {"calcular_irpf", "buscar_fuentes", "registrar_hechos", "consultar_agua", "consultar_seguros"} <= _names(
        principal["tools"]
    )
    assert "consultar_fiscal" not in _names(principal["tools"])
    assert "Asistente del hogar" in principal["system"] and "Nunca sumes" in principal["system"]
    # The specialist has ONLY its module's tools plus leer_expediente, and the module's instructions.
    assert _names(especialista["tools"]) == {
        "agua_consumo",
        "agua_reclamacion",
        "agua_cambiar_tarifa",
        "agua_roto",
        "leer_expediente",
    }
    assert "Explica la factura del agua" in especialista["system"]
    # leer_expediente shows shared facts and its own, never another module's.
    perfil = json.loads(provider.tool_results[0].content)["hechos"]
    assert perfil == {"comunidad_autonoma": "Madrid", "agua_personas": 3.0}
    assert llamadas.nombres() == ["agua_consumo"]
    contexto = llamadas.registro[0][2]
    assert contexto.case_id == "case-hogar" and contexto.perfil["seguros_poliza"] == "P-9"
    # The advisor got the memo with the citable fragment, and the trace has the specialist's calls.
    informe = provider.tool_results[-1].content
    assert "Informe del especialista en «Agua»" in informe and "[F1]" in informe
    assert [(t.tool, t.agent) for t in result.tool_trace] == [
        ("consultar_agua", "asesor"),
        ("leer_expediente", "especialista_agua"),
        ("agua_consumo", "especialista_agua"),
    ]
    assert result.usage["input_tokens"] == 40 and result.updated_case is None


def test_acting_tools_become_pending_actions_and_drafts_are_labelled(hogar) -> None:
    llamadas, modulos = hogar
    provider = RecordingProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "Cambia mi tarifa y prepara una reclamación"})),
            tool_turn(
                ("agua_cambiar_tarifa", {"x": 2}),
                ("agua_reclamacion", {"x": 1}),
                ("agua_roto", {}),
            ),
            text_turn("He dejado el cambio de tarifa pendiente de tu confirmación y un borrador [F1]."),
            text_turn("El cambio de tarifa está pendiente de tu confirmación. Te dejo un borrador [F1]."),
        ],
        [verdict("respuesta", ("Borrador", ["F1"], "respaldada"))],
    )
    result = TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run(
        "Cambia mi tarifa del agua", case=_case(), modulos=modulos
    )
    # The acting tool never ran; the failing one did not crash the turn.
    assert llamadas.nombres() == ["agua_reclamacion", "agua_roto"]
    (accion,) = result.acciones_propuestas
    assert (accion.case_id, accion.modulo, accion.herramienta, accion.argumentos) == (
        "case-hogar",
        "agua",
        "agua_cambiar_tarifa",
        {"x": 2},
    )
    assert accion.estado == "pendiente" and accion.resumen.startswith("Agua — Calcula algo del hogar")
    pendiente, borrador, roto = provider.tool_results[:3]
    assert "NO se ha ejecutado" in pendiente.content and not pendiente.is_error
    assert borrador.content.startswith("[F1] BORRADOR")
    assert roto.is_error and "fallo interno" in roto.content


def test_without_a_case_an_action_is_refused(hogar) -> None:
    llamadas, modulos = hogar
    provider = RecordingProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "Cambia mi tarifa"})),
            tool_turn(("agua_cambiar_tarifa", {})),
            text_turn("No puedo sin expediente."),
            text_turn("Necesito un expediente para dejar la acción pendiente."),
        ],
        [verdict("pregunta_aclaratoria")],
    )
    result = TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run("x", modulos=modulos)
    assert result.acciones_propuestas == [] and provider.tool_results[0].is_error
    assert llamadas.nombres() == []


def test_one_question_several_specialists_and_a_tax_tool(hogar) -> None:
    _llamadas, modulos = hogar
    provider = RecordingProvider(
        [
            tool_turn(
                ("consultar_agua", {"pregunta": "Coste del agua"}),
                ("consultar_seguros", {"pregunta": "Renovación del seguro"}),
                ("calcular_irpf", {"datos": {"rendimiento_trabajo_bruto": 30000}}),
            ),
            tool_turn(("agua_consumo", {})),
            text_turn("31,20 € al mes [F1]."),
            tool_turn(("seguros_renovacion", {})),
            text_turn("Renueva por 210,00 € [F2]."),
            text_turn("Agua: 31,20 € al mes [F1]. Seguro: 210,00 € [F2]. IRPF en [F3]."),
        ],
        [verdict("respuesta", ("agua", ["F1"], "respaldada"), ("seguro", ["F2"], "respaldada"))],
    )
    result = TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run("¿Qué pago?", modulos=modulos)
    assert [c.fragment_id for c in result.citations] == ["F1", "F2", "F3"]
    assert result.citations[2].title.startswith("Cálculo IRPF")
    assert len(provider.starts) == 3


def test_a_module_can_record_its_own_facts_in_the_case() -> None:
    llamadas = Llamadas()

    def anota(argumentos, contexto) -> str:
        contexto.registrar({"agua_personas": 4})
        return "Anotado: 4 personas."

    modulo = agua(llamadas)
    modulo.herramientas = (llamadas.herramienta("agua_consumo", contenido=anota),)
    instalar(modulo)
    provider = RecordingProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "Somos 4 en casa"})),
            tool_turn(("agua_consumo", {})),
            text_turn("Anotado [F1]."),
            text_turn("He anotado que sois 4 [F1]."),
        ],
        [verdict("respuesta", ("4 personas", ["F1"], "respaldada"))],
    )
    result = TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run(
        "Somos 4", case=_case(), modulos=[FISCAL, modulo]
    )
    assert result.updated_case is not None
    assert [(f.name, f.value, f.source) for f in result.updated_case.facts] == [("agua_personas", 4.0, "modulo:agua")]


@pytest.mark.parametrize("modulos", [None, [FISCAL]])
def test_without_household_modules_the_advisor_is_unchanged(modulos) -> None:
    provider = RecordingProvider([text_turn("¿De qué comunidad eres?")], [verdict("pregunta_aclaratoria")])
    TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run("Hola", case=_case(), modulos=modulos)
    (start,) = provider.starts
    assert start["system"] == AGENT_SYSTEM_PROMPT + CASE_TOOLS_ADDENDUM
    assert not any(name.startswith("consultar_") and name != "consultar_expediente" for name in _names(start["tools"]))


def test_the_team_agent_passes_the_modules_through(hogar) -> None:
    _llamadas, modulos = hogar
    provider = RecordingProvider([text_turn("Hola.")], [verdict("abstencion")])
    TeamAgent(provider=provider, searcher=FakeSearcher(corpus()), mode="never").run("Hola", modulos=modulos)
    assert "consultar_agua" in _names(provider.starts[0]["tools"])


def test_a_specialist_that_produces_nothing_is_an_error_for_the_advisor() -> None:
    llamadas = Llamadas()
    modulo = ModuloFalso(clave="agua", nombre="Agua", descripcion="Agua.", instrucciones="Agua.")
    instalar(modulo)
    provider = RecordingProvider(
        [
            tool_turn(("consultar_agua", {"pregunta": "x"})),
            text_turn(""),
            text_turn("No he podido consultar el agua."),
        ],
        [verdict("abstencion")],
    )
    TaxAgent(provider=provider, searcher=FakeSearcher(corpus())).run("x", modulos=[FISCAL, modulo])
    assert provider.tool_results[0].is_error and "no ha podido responder" in provider.tool_results[0].content
    assert llamadas.nombres() == []
