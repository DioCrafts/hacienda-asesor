"""Profile bridge: the ``actividad_*`` facts become ``rendimiento_actividad_neto`` of the IRPF input."""

from decimal import Decimal

import pytest

from hacienda_gpt.decision.fact_extractor import _SYSTEM_PROMPT, EXPECTED_FACT_TYPE
from hacienda_gpt.decision.schemas import FactValueType
from hacienda_gpt.tax.actividad import ActividadInput
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.profile import (
    ACTIVIDAD_FACT_FIELDS,
    ACTIVIDAD_FACT_TYPES,
    DatosInvalidosError,
    actividad_del_perfil,
    build_input,
    build_input_con_actividad,
)
from hacienda_gpt.tax.simulador import simular

D = Decimal
# 40,000 - (3,600 + 6,400) = 30,000; flat allowance 5 % = 1,500; net 28,500
ACTIVIDAD = {
    "actividad_ingresos_integros": 40000.0,
    "actividad_seguridad_social_autonomo": 3600.0,
    "actividad_alquileres": 6400.0,
    "es_autonomo": True,
    "actividad_profesional": True,
}


def test_activity_facts_are_in_the_extractor_vocabulary_with_their_descriptions() -> None:
    for name, kind in ACTIVIDAD_FACT_TYPES.items():
        assert EXPECTED_FACT_TYPE[name] == FactValueType(kind)
        assert f"    {name} " in _SYSTEM_PROMPT
    # Every numeric activity fact maps onto a field of the activity calculator, none onto the IRPF input.
    numeric = {name for name, kind in ACTIVIDAD_FACT_TYPES.items() if kind == "number"}
    assert numeric == set(ACTIVIDAD_FACT_FIELDS)
    assert set(ACTIVIDAD_FACT_FIELDS.values()) <= set(ActividadInput.model_fields)
    assert not set(ACTIVIDAD_FACT_TYPES) & set(IRPFInput.model_fields)


def test_net_income_is_computed_from_the_breakdown() -> None:
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts={**ACTIVIDAD, "retenciones": 4000.0})
    assert inp.rendimiento_actividad_neto == D("28500.00")
    assert inp.retenciones == D(4000)
    assert actividad is not None and actividad.calculado
    assert actividad.resultado is not None and actividad.resultado.gastos_dificil_justificacion == D("1500.00")
    assert "28500.00 €" in actividad.aviso and "estimación directa simplificada" in actividad.aviso
    # 28,500 of income is not "inferior a 12.000": no art. 32.2.3.º reduction, and it says how it was checked
    assert actividad.otras_rentas_no_exentas == 0 and actividad.reduccion_rendimientos_trabajo == 0
    assert "No corresponde ninguna reducción del art. 32.2 LIRPF" in actividad.aviso
    assert "primero el rendimiento de la actividad sin esa reducción" in actividad.aviso
    assert build_input(ejercicio=2026, facts=ACTIVIDAD).rendimiento_actividad_neto == D("28500.00")
    # The same breakdown works for 2025
    assert build_input(ejercicio=2025, facts=ACTIVIDAD).rendimiento_actividad_neto == D("28500.00")


def test_low_income_reduction_is_applied_from_the_case_facts() -> None:
    # No other income. 10,000 - 1,000 = 9,000; flat 450; net 8,550 -> 1,620 - 0.405 x 550 = 1,397.25 -> 7,152.75
    # (the bridge used to leave it at 8,550)
    bajo = {"actividad_ingresos_integros": 10000.0, "actividad_otros_gastos": 1000.0}
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts=bajo)
    assert inp.rendimiento_actividad_neto == D("7152.75")
    assert actividad is not None and actividad.resultado is not None
    assert actividad.resultado.reduccion_aplicada == "art. 32.2.3.º LIRPF"
    assert "Incluye 1397.25 € de reducción del art. 32.2.3.º LIRPF" in actividad.aviso
    assert "la ley no define cómo se computan" in actividad.aviso

    # With a small salary. Work: 3,000 gross - 2,000 (art. 19.2 f) = 1,000 net; art. 20 reduces it to zero
    # (reduction 1,000). Activity: 6,000 - 300 = 5,700. Income 5,700 + 1,000 = 6,700 <= 8,000 -> 1,620;
    # joint cap 3,700 - 1,000 = 2,700 does not bind -> 4,080
    inp, actividad = build_input_con_actividad(
        ejercicio=2026, facts={"actividad_ingresos_integros": 6000.0, "rendimiento_trabajo_bruto": 3000.0}
    )
    assert actividad is not None
    assert (actividad.otras_rentas_no_exentas, actividad.reduccion_rendimientos_trabajo) == (D("1000.00"), D("1000.00"))
    assert inp.rendimiento_actividad_neto == D("4080.00")

    # Interest counts as other income: 5,700 + 3,000 = 8,700 -> 1,620 - 0.405 x 700 = 1,336.50 -> 4,363.50
    inp = build_input(
        ejercicio=2026, facts={"actividad_ingresos_integros": 6000.0, "rendimiento_capital_mobiliario": 3000.0}
    )
    assert inp.rendimiento_actividad_neto == D("4363.50")
    # ...and at 12,000 or more there is nothing: 5,700 + 6,300
    inp = build_input(
        ejercicio=2026, facts={"actividad_ingresos_integros": 6000.0, "rendimiento_capital_mobiliario": 6300.0}
    )
    assert inp.rendimiento_actividad_neto == D("5700.00")


def test_joint_cap_with_the_work_income_reduction_and_the_second_pass() -> None:
    # Work 9,000 gross -> 7,000 net, wiped out by art. 20 (reduction 7,000 > 3,700): nothing left for
    # art. 32.2.3.º although the formula would give 1,620 - 0.405 x 2,800 = 486. Activity 4,000 - 200 = 3,800.
    inp, actividad = build_input_con_actividad(
        ejercicio=2026, facts={"actividad_ingresos_integros": 4000.0, "rendimiento_trabajo_bruto": 9000.0}
    )
    assert inp.rendimiento_actividad_neto == D("3800.00")
    assert actividad is not None and actividad.reduccion_rendimientos_trabajo == D("7000.00")

    # Second pass. Activity 7,400 - 370 = 7,030; work 5,000 gross -> 3,000 net.
    # First: with 7,030 of other income (> 6,500) art. 20 gives nothing, so art. 32.2.3.º would be
    # 1,620 - 0.405 x (10,030 - 8,000) = 797.85 -> activity 6,232.15.
    # But 6,232.15 <= 6,500 brings art. 20 back (reduction 3,000), and 3,000 + 797.85 > 3,700:
    # art. 32.2.3.º is cut to 700 -> activity 6,330, still <= 6,500, joint total exactly 3,700.
    inp, actividad = build_input_con_actividad(
        ejercicio=2026, facts={"actividad_ingresos_integros": 7400.0, "rendimiento_trabajo_bruto": 5000.0}
    )
    assert actividad is not None and actividad.resultado is not None
    assert actividad.resultado.reduccion_art_32_2 == D("700.00")
    assert actividad.reduccion_rendimientos_trabajo == D("3000.00")
    assert inp.rendimiento_actividad_neto == D("6330.00")
    from hacienda_gpt.tax.irpf import calcular_irpf

    final = calcular_irpf(inp)
    reduccion_art_20 = -next(
        line.importe for line in final.lineas if line.concepto.startswith("Reducción por obtención")
    )
    assert reduccion_art_20 + actividad.resultado.reduccion_art_32_2 == D("3700.00")


def test_dependent_reduction_only_when_declared_and_compatible_with_the_case() -> None:
    # 20,000 - 4,000 = 16,000; 2,000 + [6,498 - 1.14 x 1,952.5] = 6,272.15 -> 9,727.85 (no flat allowance)
    base = {"actividad_ingresos_integros": 20000.0, "actividad_otros_gastos": 4000.0}
    declarado = {**base, "actividad_cumple_requisitos_dependiente": True}
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts=declarado)
    assert inp.rendimiento_actividad_neto == D("9727.85")
    assert actividad is not None and "que has declarado que te corresponde" in actividad.aviso
    # Not declared: flat allowance 800 -> 15,200, and the result invites to declare it
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts=base)
    assert inp.rendimiento_actividad_neto == D("15200.00")
    assert actividad is not None and "Si trabajas para un único cliente" in actividad.aviso
    # Disability from the profile ("igual o superior al 33 por ciento"): + 3,500 -> 6,227.85; 65 %: + 7,750 -> 1,977.85
    assert build_input(ejercicio=2026, facts={**declarado, "grado_discapacidad": 33.0}).rendimiento_actividad_neto == D(
        "6227.85"
    )
    assert build_input(ejercicio=2026, facts={**declarado, "grado_discapacidad": 65.0}).rendimiento_actividad_neto == D(
        "1977.85"
    )
    assert build_input(ejercicio=2026, facts={**declarado, "grado_discapacidad": 32.0}).rendimiento_actividad_neto == D(
        "9727.85"
    )
    # Other income above 6,500 loses the additional amount: only the 2,000 -> 14,000
    assert build_input(
        ejercicio=2026, facts={**declarado, "rendimiento_capital_mobiliario": 6500.01}
    ).rendimiento_actividad_neto == D("14000.00")
    # A salary above 4,000 contradicts requirement e): refused, general rules (15,200; income >= 12,000)
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts={**declarado, "rendimiento_trabajo_bruto": 5000.0})
    assert inp.rendimiento_actividad_neto == D("15200.00")
    assert actividad is not None and "no se aplica la reducción de autónomo dependiente" in actividad.aviso.lower()


def test_irregular_income_new_activity_and_ceuta_come_from_the_facts() -> None:
    # 60,000 - 10,000 -> flat 2,000 -> 48,000; art. 32.1: 30 % x 20,000 = 6,000; art. 32.3: 20 % x 42,000 = 8,400
    facts = {
        "actividad_ingresos_integros": 60000.0,
        "actividad_otros_gastos": 10000.0,
        "actividad_rendimientos_irregulares": 20000.0,
        "actividad_reduccion_inicio": True,
    }
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts=facts)
    assert inp.rendimiento_actividad_neto == D("33600.00")
    assert actividad is not None and "por 14400.00 € en total" in actividad.aviso
    # Ceuta 2026: 30,000 - 10,000 = 20,000; 10 % = 2,000 -> 18,000
    ceuta = {
        "actividad_ingresos_integros": 30000.0,
        "actividad_otros_gastos": 10000.0,
        "actividad_ceuta_melilla": "Ceuta",
    }
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts=ceuta)
    assert inp.rendimiento_actividad_neto == D("18000.00")
    assert actividad is not None and actividad.resultado is not None
    assert any("simulador de IRPF" in aviso for aviso in actividad.resultado.avisos)
    with pytest.raises(DatosInvalidosError, match="ceuta_melilla"):
        build_input(ejercicio=2026, facts={**ceuta, "actividad_ceuta_melilla": "Canarias"})


def test_without_activity_figures_nothing_changes() -> None:
    assert actividad_del_perfil(ejercicio=2026, facts={"rendimiento_trabajo_bruto": 30000.0}) is None
    # The flags alone are not figures
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts={"es_autonomo": True})
    assert actividad is None and inp.rendimiento_actividad_neto == 0
    inp, actividad = build_input_con_actividad(ejercicio=2026, facts={"rendimiento_actividad_neto": 12000.0})
    assert actividad is None and inp.rendimiento_actividad_neto == D(12000)


def test_an_explicit_net_figure_wins_in_its_own_or_a_more_specific_layer() -> None:
    # Same layer: the explicit figure is used and the result says the breakdown was ignored
    inp, actividad = build_input_con_actividad(
        ejercicio=2026, facts={**ACTIVIDAD, "rendimiento_actividad_neto": 20000.0}
    )
    assert inp.rendimiento_actividad_neto == D(20000)
    assert actividad is not None and not actividad.calculado and actividad.resultado is None
    assert "indicado directamente" in actividad.aviso
    # Breakdown saved in the case, explicit figure typed for this simulation
    inp, actividad = build_input_con_actividad(
        ejercicio=2026, facts=ACTIVIDAD, datos={"rendimiento_actividad_neto": 5000}
    )
    assert inp.rendimiento_actividad_neto == D(5000) and actividad is not None and not actividad.calculado


def test_a_breakdown_in_a_more_specific_layer_overrides_a_stored_net_figure() -> None:
    # Stored: a net figure and a breakdown. The scenario raises the income to 50,000:
    # 50,000 - 10,000 = 40,000; 5 % = 2,000; net 38,000
    inp, actividad = build_input_con_actividad(
        ejercicio=2026,
        facts={**ACTIVIDAD, "rendimiento_actividad_neto": 20000.0},
        cambios={"actividad_ingresos_integros": 50000},
    )
    assert inp.rendimiento_actividad_neto == D("38000.00")
    assert actividad is not None and actividad.calculado


def test_activity_names_are_accepted_in_datos_and_bad_values_are_rejected() -> None:
    # 10,000 - 1,000 = 9,000; 5 % = 450; net 8,550; art. 32.2.3.º: 1,397.25 -> 7,152.75
    inp = build_input(ejercicio=2026, datos={"actividad_ingresos_integros": 10000, "actividad_otros_gastos": 1000})
    assert inp.rendimiento_actividad_neto == D("7152.75")
    with pytest.raises(DatosInvalidosError, match="actividad"):
        build_input(ejercicio=2026, datos={"actividad_ingresos_integros": -1})
    with pytest.raises(DatosInvalidosError, match="actividad_ingresos"):
        build_input(ejercicio=2026, datos={"actividad_ingresos": 100})


def test_a_year_without_self_employed_parameters_is_not_computed_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every year with IRPF parameters has self-employed ones today; simulate one that does not.
    from hacienda_gpt.tax import actividad as modulo
    from hacienda_gpt.tax.params import EjercicioNoDisponibleError

    def sin_parametros(ejercicio: int):
        raise EjercicioNoDisponibleError(f"No hay parámetros de autónomos cargados para {ejercicio}.")

    monkeypatch.setattr(modulo, "load_autonomos_params", sin_parametros)
    inp, actividad = build_input_con_actividad(ejercicio=2025, facts=ACTIVIDAD)
    assert inp.rendimiento_actividad_neto == 0
    assert actividad is not None and not actividad.calculado
    assert "NO está incluido" in actividad.aviso and "2025" in actividad.aviso


def test_simulation_carries_the_warning_first_and_the_detail() -> None:
    sim = simular(
        ejercicio=2026,
        facts={**ACTIVIDAD, "comunidad_autonoma": "Madrid"},
        escenarios=[
            {"nombre": "Tu expediente", "cambios": {}},
            {"nombre": "Más gastos", "cambios": {"actividad_otros_gastos": 5000}},
        ],
    )
    base, alt = sim.escenarios
    assert base.actividad is not None and base.actividad.calculado
    assert base.resultado.avisos[0] == base.actividad.aviso
    assert base.resultado.base_imponible_general == D("28500.00")
    assert base.actividad.resultado is not None
    assert any(line.concepto.startswith("Provisiones deducibles") for line in base.actividad.resultado.lineas)
    # 40,000 - 15,000 = 25,000; 5 % = 1,250; net 23,750: less income, less tax
    assert alt.actividad is not None and alt.actividad.resultado is not None
    assert alt.actividad.resultado.rendimiento_actividad_neto == D("23750.00")
    assert alt.resultado.base_imponible_general == D("23750.00")
    assert alt.ahorro_frente_a_base > 0
    # Known through its breakdown, so the lever catalogue does not treat the activity as missing
    assert "retenciones" in sim.datos_no_aportados

    # A profile without activity is untouched
    plain = simular(ejercicio=2026, facts={"rendimiento_trabajo_bruto": 30000.0})
    assert plain.escenarios[0].actividad is None
    assert not any("actividad" in aviso and "calculado" in aviso for aviso in plain.escenarios[0].resultado.avisos)


# --- The agent and the document reader ------------------------------------------------------------


class _NoSearch:
    def search(self, query, *, k, filters):
        return []


def _toolbox(**facts):
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.tools import Toolbox
    from hacienda_gpt.decision.schemas import CaseState, Fact

    case = CaseState(
        case_id="c",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        facts=[
            Fact(fact_id=f"f_{k}", name=k, value=v, value_type=EXPECTED_FACT_TYPE[k], source="t", confidence=1.0)
            for k, v in facts.items()
        ],
    )
    return Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger(), case=case)


def test_agent_tools_say_that_the_activity_income_was_derived() -> None:
    import json

    from hacienda_gpt.agent.providers.base import ToolCall

    bajo = {"actividad_ingresos_integros": 10000.0, "actividad_otros_gastos": 1000.0}
    toolbox = _toolbox(**bajo)
    payload = json.loads(toolbox.execute(ToolCall(id="1", name="listar_beneficios_fiscales", arguments={})).content)
    assert "7152.75 €" in payload["actividad_economica"]
    assert "art. 32.2.3.º LIRPF" in payload["actividad_economica"]

    outcome = toolbox.execute(ToolCall(id="2", name="comparar_declaracion_conjunta", arguments={"conyuge_b": {}}))
    assert not outcome.is_error
    assert "Cónyuge A: El rendimiento neto de la actividad (7152.75 €) se ha calculado" in outcome.content

    outcome = toolbox.execute(ToolCall(id="3", name="calcular_irpf", arguments={}))
    assert "El rendimiento neto de la actividad (7152.75 €) se ha calculado" in outcome.content

    # A profile without activity says nothing about it
    payload = json.loads(
        _toolbox(rendimiento_trabajo_bruto=30000.0)
        .execute(ToolCall(id="4", name="listar_beneficios_fiscales", arguments={}))
        .content
    )
    assert "actividad_economica" not in payload


def test_documents_can_propose_the_activity_figures_with_a_literal_quote() -> None:
    from hacienda_gpt.tax.documentos import (
        DOCUMENT_ACTIVITY_FACTS,
        DOCUMENT_FACTS,
        SCHEMA,
        SYSTEM_PROMPT,
        _hechos_validos,
        _normalize,
    )

    assert set(DOCUMENT_ACTIVITY_FACTS) <= set(DOCUMENT_FACTS)
    assert "actividad_ingresos_integros" in DOCUMENT_ACTIVITY_FACTS
    # What only the user can declare is never read off a document
    for name in (
        "actividad_cumple_requisitos_dependiente",
        "actividad_reduccion_inicio",
        "actividad_rendimientos_irregulares",
    ):
        assert name not in DOCUMENT_FACTS
    assert set(DOCUMENT_ACTIVITY_FACTS) <= set(SCHEMA["properties"]["hechos"]["items"]["properties"]["name"]["enum"])
    for name in DOCUMENT_ACTIVITY_FACTS:
        assert name in SYSTEM_PROMPT

    documento = (
        "LIBRO REGISTRO DE INGRESOS 2026\nTotal base imponible del ejercicio: 41.250,00\nTotal cuotas RETA: 3.600,00"
    )
    raw = {
        "hechos": [
            {
                "name": "actividad_ingresos_integros",
                "value": "41250.00",
                "evidencia": "Total base imponible del ejercicio: 41.250,00",
            },
            # Invented passage: discarded by the literal-quote safeguard
            {"name": "actividad_suministros", "value": "900", "evidencia": "Total suministros: 900,00"},
            {
                "name": "actividad_cumple_requisitos_dependiente",
                "value": "true",
                "evidencia": "LIBRO REGISTRO DE INGRESOS 2026",
            },
        ]
    }
    propuestos, descartados = _hechos_validos(raw, _normalize(documento))
    assert set(propuestos) == {"actividad_ingresos_integros"}
    assert propuestos["actividad_ingresos_integros"].value == 41250.0
    assert descartados == ["actividad_suministros"]
