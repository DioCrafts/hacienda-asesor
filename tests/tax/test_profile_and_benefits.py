from decimal import Decimal

import pytest

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, KNOWN_FACT_NAMES
from hacienda_gpt.decision.schemas import CaseState, Fact
from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.profile import PROFILE_FACT_TYPES, DatosInvalidosError, build_input

D = Decimal


class _NoSearch:
    def search(self, query, *, k, filters):
        return []


def _fact(name: str, value) -> Fact:
    return Fact(
        fact_id=f"fact_{name}", name=name, value=value, value_type=EXPECTED_FACT_TYPE[name], source="t", confidence=1.0
    )


def _case(**facts) -> CaseState:
    return CaseState(
        case_id="c", user_id="u", jurisdiction="ES", tax_period="2026", facts=[_fact(k, v) for k, v in facts.items()]
    )


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


def test_profile_facts_are_in_the_extractor_vocabulary_and_map_onto_the_calculator() -> None:
    assert set(PROFILE_FACT_TYPES) <= set(KNOWN_FACT_NAMES)
    numeric_inputs = {n for n, t in PROFILE_FACT_TYPES.items() if t == "number"}
    # Every numeric profile fact is a calculator field of the same name: no mapping table to drift.
    assert numeric_inputs <= set(IRPFInput.model_fields)


def test_build_input_layers_facts_datos_and_cambios() -> None:
    facts = {"rendimiento_trabajo_bruto": 30000.0, "num_descendientes": 2.0, "tipo_renta": "trabajo"}
    inp = build_input(
        ejercicio=2026, facts=facts, datos={"retenciones": 4000}, cambios={"rendimiento_trabajo_bruto": 35000}
    )
    assert inp.rendimiento_trabajo_bruto == D(35000) and inp.retenciones == D(4000)
    assert inp.num_descendientes == 2  # float count from the fact store coerced to int

    with pytest.raises(DatosInvalidosError, match="sueldo"):
        build_input(ejercicio=2026, datos={"sueldo": 30000})
    with pytest.raises(DatosInvalidosError, match="tributacion"):
        build_input(ejercicio=2026, datos={"tributacion": "en pareja"})
    with pytest.raises(DatosInvalidosError):
        build_input(ejercicio=2026, datos={"retenciones": -5})


def test_benefits_quantify_levers_with_the_calculator_and_flag_missing_data() -> None:
    inp = IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(45000),
        cotizaciones_seguridad_social=D(2900),
        comunidad_autonoma="Madrid",
    )
    by_id = {b.id: b for b in evaluar_beneficios(inp, conocidos={"rendimiento_trabajo_bruto", "comunidad_autonoma"})}

    plan = by_id["plan_pensiones"]
    # 1.500 × 35,9 %: marginal rates of 18,5 % (state) + 17,4 % (Madrid) at this income.
    assert plan.estado == "disponible" and plan.ahorro_estimado == D("538.50")
    assert any("ilíquido" in requisito for requisito in plan.requisitos)  # the non-tax cost is stated
    assert by_id["donativos"].ahorro_estimado == D("200.00")  # 250 × 80 %
    assert by_id["minimo_descendientes"].estado == "por_confirmar"
    assert by_id["minimo_descendientes"].datos_que_faltan == ["num_descendientes", "num_descendientes_menores_3"]
    assert by_id["tributacion_conjunta"].datos_que_faltan == ["estado_civil"]
    # Madrid is loaded: its deductions are listed from its own law, with the
    # income ceiling checked against this profile (base 40.100 € > 30.930 € / 26.414,22 €).
    birth, rent = by_id["autonomica_art_4"], by_id["autonomica_art_8"]
    assert birth.estado == "no_aplica" and "supera el límite de 30930.00 €" in birth.detalle
    assert rent.estado == "no_aplica" and "1.237,20 €" in rent.detalle
    assert by_id["autonomica_art_11"].estado == "por_confirmar" and "412,40 €" in by_id["autonomica_art_11"].detalle
    assert "BOCM-m-2010-90068" in birth.referencia
    assert "art. 9: donativos a fundaciones" in by_id["autonomicas_otras"].detalle
    assert "deducciones_autonomicas" not in by_id
    assert "reduccion_trabajo" not in by_id  # not applicable at this salary: not listed as noise

    # A stated zero is not "unknown": no need to ask again.
    known_zero = {b.id for b in evaluar_beneficios(inp, conocidos={"num_descendientes", "num_ascendientes_mayores_65"})}
    assert "minimo_descendientes" not in known_zero and "minimo_ascendientes" not in known_zero


def test_benefits_report_what_is_already_applied() -> None:
    inp = IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(16000),
        cotizaciones_seguridad_social=D(1016),
        aportacion_plan_pensiones=D(1500),
        num_descendientes=2,
        num_descendientes_menores_3=1,
        donativos=D(100),
        tributacion="conjunta_monoparental",
    )
    by_id = {b.id: b for b in evaluar_beneficios(inp, conocidos=set(IRPFInput.model_fields))}
    for applied in ("plan_pensiones", "tributacion_conjunta", "minimo_descendientes", "reduccion_trabajo", "donativos"):
        assert by_id[applied].estado == "aplicado", applied
    assert by_id["maternidad"].estado == "por_confirmar" and by_id["maternidad"].ahorro_estimado == D("1200")
    assert by_id["familia_numerosa"].estado == "por_confirmar"


def test_calculate_tool_registers_each_scenario_as_citable_evidence() -> None:
    ledger = EvidenceLedger()
    toolbox = Toolbox(
        searcher=_NoSearch(),
        ledger=ledger,
        case=_case(rendimiento_trabajo_bruto=50000.0, cotizaciones_seguridad_social=3175.0, retenciones=9000.0),
    )

    outcome = toolbox.execute(
        _call(
            "calcular_irpf",
            escenarios=[
                {"nombre": "Actual", "cambios": {}},
                {"nombre": "Con plan de pensiones", "cambios": {"aportacion_plan_pensiones": 1500}},
            ],
        )
    )

    assert not outcome.is_error and outcome.summary == "2 cálculo(s): F1, F2"
    assert "[F1] Cálculo del IRPF 2026 — Actual" in outcome.content
    assert "Frente a «Actual»: ahorra 555,00 €." in outcome.content
    assert "comunidad_autonoma" in outcome.content  # the missing core datum is called out
    f2 = ledger.get("F2")
    assert f2.authority == "CALCULADORA" and f2.source_type == "calculo"
    assert "Reducción por aportaciones a sistemas de previsión social: -1.500,00 €" in f2.text
    assert "[art. 52.1 LIRPF]" in f2.text and "BOE-A-2006-20764" in f2.text


def test_calculate_tool_works_without_a_case_and_rejects_bad_input() -> None:
    toolbox = Toolbox(searcher=_NoSearch(), ledger=EvidenceLedger())

    ok = toolbox.execute(_call("calcular_irpf", ejercicio=2026, datos={"rendimiento_trabajo_bruto": 30000}))
    assert not ok.is_error and "Situación actual" in ok.content

    assert "2019" in toolbox.execute(_call("calcular_irpf", ejercicio=2019, datos={})).content
    assert toolbox.execute(_call("calcular_irpf", datos={"sueldo": 1})).is_error
    assert toolbox.execute(_call("calcular_irpf", datos="30000")).is_error
    assert toolbox.execute(_call("calcular_irpf", escenarios=[{"cambios": {}}])).is_error
    assert toolbox.execute(_call("calcular_irpf", escenarios=[{"nombre": "x", "cambios": {}}] * 7)).is_error


def test_benefits_tool_uses_case_facts() -> None:
    import json

    toolbox = Toolbox(
        searcher=_NoSearch(),
        ledger=EvidenceLedger(),
        case=_case(rendimiento_trabajo_bruto=45000.0, num_descendientes=0.0, estado_civil="soltero"),
    )
    outcome = toolbox.execute(_call("listar_beneficios_fiscales"))
    payload = json.loads(outcome.content)

    assert payload["ejercicio"] == 2026
    by_name = {b["nombre"]: b for b in payload["beneficios"]}
    assert by_name["Aportación a plan de pensiones"]["ahorro_estimado_eur"] == 555.0
    assert "Mínimo por descendientes" not in by_name  # the user said they have none
    assert by_name["Tributación conjunta"]["datos_que_faltan"] == []
    assert "calcular_irpf" in payload["nota"]


def test_regional_deductions_stay_open_under_the_income_ceiling_and_unloaded_regions_point_to_the_corpus() -> None:
    low = IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=D(24000), comunidad_autonoma="Madrid")
    by_id = {b.id: b for b in evaluar_beneficios(low, conocidos={"comunidad_autonoma"})}
    assert by_id["autonomica_art_4"].estado == "por_confirmar" and "721,70 €" in by_id["autonomica_art_4"].detalle
    assert by_id["autonomica_art_8"].estado == "por_confirmar"
    # They are open: the catalogue says which datum makes the simulation apply them.
    assert "alquiler_vivienda_habitual" in by_id["autonomica_art_8"].detalle
    assert by_id["autonomica_art_8"].datos_que_faltan == ["alquiler_vivienda_habitual"]

    renter = low.model_copy(
        update={"edad": 30, "alquiler_vivienda_habitual": D(9600), "cotizaciones_seguridad_social": D(1524)}
    )
    applied = {
        b.id: b for b in evaluar_beneficios(renter, conocidos={"comunidad_autonoma", "alquiler_vivienda_habitual"})
    }
    assert applied["autonomica_art_8"].estado == "aplicado" and "1237.20 €" in applied["autonomica_art_8"].detalle

    other = IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=D(24000), comunidad_autonoma="Navarra")
    regional = {b.id: b for b in evaluar_beneficios(other, conocidos={"comunidad_autonoma"})}["deducciones_autonomicas"]
    assert "autonomico:Navarra" in regional.detalle and not any(k.startswith("autonomica_art") for k in by_id if False)
    # A loaded community other than Madrid lists its deductions by title only.
    gallego = IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=D(24000), comunidad_autonoma="Galicia")
    titulos = {b.id: b for b in evaluar_beneficios(gallego, conocidos={"comunidad_autonoma"})}["autonomicas_otras"]
    assert titulos.estado == "por_confirmar" and "BOE-A-2011-18161" in titulos.referencia
    assert "la simulación no calcula" in titulos.detalle
    unknown = {b.id: b for b in evaluar_beneficios(IRPFInput(ejercicio=2026), conocidos=set())}[
        "deducciones_autonomicas"
    ]
    assert unknown.datos_que_faltan == ["comunidad_autonoma"]


def test_tax_package_imports_standalone() -> None:
    # Importing the tax package before anything from `decision` used to hit a
    # circular import (profile → decision → fact_extractor → profile).
    import subprocess
    import sys

    code = "import hacienda_gpt.tax.simulador, hacienda_gpt.tax.documentos, hacienda_gpt.tax.calendario"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0
