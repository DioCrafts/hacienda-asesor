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
    assert "sin aplicar las reducciones del art. 32 LIRPF" in actividad.aviso
    assert build_input(ejercicio=2026, facts=ACTIVIDAD).rendimiento_actividad_neto == D("28500.00")


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
    # 10,000 - 1,000 = 9,000; 5 % = 450; net 8,550
    inp = build_input(ejercicio=2026, datos={"actividad_ingresos_integros": 10000, "actividad_otros_gastos": 1000})
    assert inp.rendimiento_actividad_neto == D("8550.00")
    with pytest.raises(DatosInvalidosError, match="actividad"):
        build_input(ejercicio=2026, datos={"actividad_ingresos_integros": -1})
    with pytest.raises(DatosInvalidosError, match="actividad_ingresos"):
        build_input(ejercicio=2026, datos={"actividad_ingresos": 100})


def test_a_year_without_self_employed_parameters_is_not_computed_and_says_so() -> None:
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
