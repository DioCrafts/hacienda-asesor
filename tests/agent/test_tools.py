import json

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.decision.schemas import CaseState, MissingFact, RiskLevel
from .fakes import FakeSearcher, corpus


def _case() -> CaseState:
    return CaseState(
        case_id="case_1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        missing_facts=[MissingFact(fact_name="residencia_fiscal", reason="r", priority=RiskLevel.HIGH)],
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


def test_search_registers_fragments_and_passes_filters() -> None:
    searcher = FakeSearcher(corpus())
    toolbox = Toolbox(searcher=searcher, ledger=EvidenceLedger())

    outcome = toolbox.execute(
        _call("buscar_fuentes", consulta="plazo reinversión", tipo_fuente="doctrina_administrativa", max_resultados=99)
    )

    assert not outcome.is_error
    assert "[F1]" in outcome.content and "dos años" in outcome.content
    assert outcome.summary == "1 fragmentos: F1"
    query, k, filters = searcher.calls[0]
    assert (query, k) == ("plazo reinversión", 10)  # clamped to the max
    assert filters.source_type == "doctrina_administrativa"


def test_search_validates_arguments_and_reports_empty_results() -> None:
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger())

    assert toolbox.execute(_call("buscar_fuentes", consulta="  ")).is_error
    assert toolbox.execute(_call("buscar_fuentes", consulta="x", tipo_fuente="blog")).is_error
    empty = toolbox.execute(_call("buscar_fuentes", consulta="x", solo_vinculante=True))
    assert not empty.is_error and "Sin resultados" in empty.content and "sin filtros" in empty.content


def test_malformed_call_unknown_tool_and_searcher_crash_become_error_results() -> None:
    class Boom:
        def search(self, query, *, k, filters):
            raise RuntimeError("índice no cargado")

    toolbox = Toolbox(searcher=Boom(), ledger=EvidenceLedger())

    bad_json = ToolCall(id="c", name="buscar_fuentes", arguments={}, arguments_error="JSON no válido")
    assert toolbox.execute(bad_json).is_error
    assert toolbox.execute(_call("borrar_todo")).is_error
    crashed = toolbox.execute(_call("buscar_fuentes", consulta="x"))
    assert crashed.is_error and "índice no cargado" in crashed.content


def test_case_tools_only_offered_with_a_case() -> None:
    without = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger())
    stateless = {
        "buscar_fuentes",
        "calcular_irpf",
        "listar_beneficios_fiscales",
        "calendario_fiscal",
        "comparar_declaracion_conjunta",
        "calcular_rendimiento_actividad",
        "calcular_modelo_130",
        "calcular_modelo_303",
    }
    assert {spec.name for spec in without.specs()} == stateless
    assert without.execute(_call("consultar_expediente")).is_error

    with_case = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger(), case=_case())
    assert {spec.name for spec in with_case.specs()} == stateless | {
        "consultar_expediente",
        "registrar_hechos",
        "evaluar_obligaciones",
    }


def test_record_facts_coerces_merges_and_keeps_case_valid() -> None:
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger(), case=_case())

    outcome = toolbox.execute(
        _call(
            "registrar_hechos",
            hechos=[
                {"nombre": "residencia_fiscal", "valor": "ES"},
                {"nombre": "periodo_fiscal", "valor": 2024},
                {"nombre": "volumen_facturacion", "valor": "45.000 €"},
                {"nombre": "alta_actividad_economica", "valor": "sí"},
                {"nombre": "plantilla", "valor": "muchos"},
                {"nombre": "color_favorito", "valor": "azul"},
            ],
        )
    )

    assert not outcome.is_error
    assert "plantilla" in outcome.content and "color_favorito" in outcome.content  # rejected, and said so
    assert toolbox.case_changed
    case = toolbox.case
    facts = {fact.name: fact.value for fact in case.facts}
    assert facts == {
        "residencia_fiscal": "ES",
        "periodo_fiscal": "2024",
        "volumen_facturacion": 45000.0,
        "alta_actividad_economica": True,
    }
    assert case.tax_period == "2024"
    assert case.missing_facts == []
    # Round-trips through the validators the store applies on load.
    CaseState.model_validate_json(case.model_dump_json())


def test_record_facts_with_nothing_valid_is_an_error_and_leaves_case_untouched() -> None:
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger(), case=_case())
    outcome = toolbox.execute(_call("registrar_hechos", hechos=[{"nombre": "plantilla", "valor": "x"}]))
    assert outcome.is_error and not toolbox.case_changed


def test_read_case_and_evaluate_rules() -> None:
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=EvidenceLedger(), case=_case())
    toolbox.execute(
        _call(
            "registrar_hechos",
            hechos=[
                {"nombre": "residencia_fiscal", "valor": "ES"},
                {"nombre": "periodo_fiscal", "valor": "2025"},
                {"nombre": "tipo_renta", "valor": "trabajo"},
                {"nombre": "menciona_ingresos", "valor": True},
            ],
        )
    )

    snapshot = json.loads(toolbox.execute(_call("consultar_expediente")).content)
    assert snapshot["ejercicio"] == "2025" and snapshot["hechos"]["tipo_renta"] == "trabajo"

    evaluated = json.loads(toolbox.execute(_call("evaluar_obligaciones")).content)
    assert [o["id"] for o in evaluated["obligaciones_candidatas"]] == ["obl_irpf_candidate"]
    assert evaluated["obligaciones_candidatas"][0]["fuentes_de_la_regla"]
    assert any(r["regla"] == "rule_iva_candidate_activity" for r in evaluated["reglas_no_activadas"])
    assert "parcial" in evaluated["aviso"]
    assert [o.obligation_id for o in toolbox.case.obligation_candidates] == ["obl_irpf_candidate"]


def test_search_date_filter_is_validated_and_forwarded() -> None:
    searcher = FakeSearcher(corpus())
    toolbox = Toolbox(searcher=searcher, ledger=EvidenceLedger())

    assert toolbox.execute(_call("buscar_fuentes", consulta="x", desde_fecha="el año pasado")).is_error
    outcome = toolbox.execute(_call("buscar_fuentes", consulta="reinversión", desde_fecha="2025-06-01"))

    assert searcher.calls[0][2].date_from == "2025-06-01"
    assert "V0058-25" not in outcome.content  # the January 2025 consulta is filtered out
