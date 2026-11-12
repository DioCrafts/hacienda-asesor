"""Savings-base offsetting (art. 49 LIRPF) and joint vs separate returns."""

from decimal import Decimal

import pytest

from hacienda_gpt.tax.compensacion import compensar_ahorro
from hacienda_gpt.tax.conjunta import comparar_tributacion
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import load_params

D = Decimal
L = D("0.25")


def test_the_limit_is_the_laws() -> None:
    assert load_params(2026).compensacion_ahorro.limite_cruzado == L


@pytest.mark.parametrize(
    ("rend", "gan", "base", "pend_r", "pend_g"),
    [
        (1000, 2000, 3000, 0, 0),  # both positive: nothing to offset
        (4000, -3000, 3000, 0, 2000),  # losses take 25 % of 4.000 = 1.000; 2.000 carried forward
        (4000, -500, 3500, 0, 0),  # small loss fits within the limit
        (-3000, 4000, 3000, 2000, 0),  # the same the other way round
        (-1000, -2000, 0, 1000, 2000),  # both negative: all carried forward
        (0, -5000, 0, 0, 5000),
    ],
)
def test_offsetting_within_the_year(rend: int, gan: int, base: int, pend_r: int, pend_g: int) -> None:
    c = compensar_ahorro(rendimientos=D(rend), ganancias=D(gan), limite_cruzado=L)
    assert (c.base_ahorro, c.rendimientos_negativos_pendientes, c.perdidas_pendientes) == (
        D(base),
        D(pend_r),
        D(pend_g),
    )


def test_losses_from_earlier_years() -> None:
    # Own kind first, without limit: 3.000 of gains absorb 3.000 of the 5.000 of old losses.
    # The other 2.000 go against capital income, capped at 25 % of 2.000 = 500.
    c = compensar_ahorro(rendimientos=D(2000), ganancias=D(3000), perdidas_anteriores=D(5000), limite_cruzado=L)
    assert c.base_ahorro == D(1500) and c.perdidas_pendientes == D(1500) and c.compensado_de_anteriores == D(3500)
    # The year's own loss uses the limit first; old losses get what is left of it (nothing here).
    c = compensar_ahorro(rendimientos=D(4000), ganancias=D(-1000), perdidas_anteriores=D(2000), limite_cruzado=L)
    assert c.base_ahorro == D(3000) and c.perdidas_pendientes == D(2000)
    # Old negative capital income against this year's capital income.
    c = compensar_ahorro(
        rendimientos=D(800), ganancias=D(0), rendimientos_negativos_anteriores=D(300), limite_cruzado=L
    )
    assert c.base_ahorro == D(500) and c.rendimientos_negativos_pendientes == 0


def test_calculator_applies_it_and_reports_what_is_left() -> None:
    base = dict(ejercicio=2026, rendimiento_trabajo_bruto=D(30000), cotizaciones_seguridad_social=D(1900))
    r = calcular_irpf(IRPFInput(**base, rendimiento_capital_mobiliario=D(4000), ganancias_patrimoniales=D(-3000)))
    assert r.base_imponible_ahorro == D("3000.00")  # not 1.000: the loss is capped at 25 % of 4.000
    assert r.perdidas_patrimoniales_a_compensar == D("2000.00") and r.rendimientos_negativos_a_compensar == 0
    assert any("Quedan saldos negativos" in a for a in r.avisos)
    # Realising a loss before year end lowers the tax: the classic lever.
    sin_perdida = calcular_irpf(IRPFInput(**base, ganancias_patrimoniales=D(6000)))
    con_perdida = calcular_irpf(IRPFInput(**base, ganancias_patrimoniales=D(2000)))
    assert con_perdida.cuota_liquida < sin_perdida.cuota_liquida
    # Losses brought forward reduce this year's gains.
    con_pendientes = calcular_irpf(
        IRPFInput(**base, ganancias_patrimoniales=D(6000), perdidas_patrimoniales_pendientes=D(4000))
    )
    assert (
        con_pendientes.base_imponible_ahorro == D("2000.00")
        and con_pendientes.cuota_liquida == con_perdida.cuota_liquida
    )


def test_joint_filing_wins_when_one_spouse_has_no_income() -> None:
    a = IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(38000),
        cotizaciones_seguridad_social=D(2400),
        retenciones=D(6500),
        num_descendientes=2,
    )
    b = IRPFInput(ejercicio=2026, num_descendientes=2)
    c = comparar_tributacion(a, b)
    assert c.mejor == "conjunta" and c.ahorro > 0
    assert c.total_individual - c.total_conjunta == c.ahorro
    assert c.conjunta.tributacion == "conjunta_biparental"
    # Children are counted once in the joint return, and halved in each individual one.
    assert c.conjunta.minimo_personal_familiar > c.individual_a.minimo_personal_familiar


def test_separate_filing_wins_with_two_similar_salaries() -> None:
    a = IRPFInput(
        ejercicio=2026,
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(40000),
        cotizaciones_seguridad_social=D(2540),
    )
    b = IRPFInput(
        ejercicio=2026,
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(36000),
        cotizaciones_seguridad_social=D(2290),
    )
    c = comparar_tributacion(a, b)
    assert c.mejor == "individual" and c.ahorro > D(1000)
    # The joint return adds both incomes.
    assert c.conjunta.base_imponible_general > c.individual_a.base_imponible_general


def test_joint_comparison_is_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    ledger = EvidenceLedger()
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=ledger)
    assert "comparar_declaracion_conjunta" in {spec.name for spec in toolbox.specs()}
    outcome = toolbox.execute(
        ToolCall(
            id="1",
            name="comparar_declaracion_conjunta",
            arguments={
                "ejercicio": 2026,
                "conyuge_a": {
                    "rendimiento_trabajo_bruto": 38000,
                    "cotizaciones_seguridad_social": 2400,
                    "num_descendientes": 2,
                },
                "conyuge_b": {},
            },
        )
    )
    assert not outcome.is_error and "Sale mejor la declaración conjunta" in outcome.content
    assert len(ledger) == 4  # the verdict and the three returns are all citable
    assert toolbox.execute(ToolCall(id="2", name="comparar_declaracion_conjunta", arguments={})).is_error


def test_self_employed_calculators_are_available_to_the_agent() -> None:
    from hacienda_gpt.agent.evidence import EvidenceLedger
    from hacienda_gpt.agent.providers.base import ToolCall
    from hacienda_gpt.agent.tools import Toolbox
    from tests.agent.fakes import FakeSearcher

    ledger = EvidenceLedger()
    toolbox = Toolbox(searcher=FakeSearcher([]), ledger=ledger)
    # 40.000 − 10.000 = 30.000; difficult-to-justify expenses: 5 % = 1.500 → net 28.500.
    actividad = toolbox.execute(
        ToolCall(
            id="1", name="calcular_rendimiento_actividad", arguments={"ingresos_integros": 40000, "otros_gastos": 10000}
        )
    )
    assert not actividad.is_error and "rendimiento_actividad_neto = 28500" in actividad.content
    # First quarter: 20 % of (10.000 − 2.000 − 5 %) = 1.520, less 600 of withholdings.
    pago = toolbox.execute(
        ToolCall(
            id="2",
            name="calcular_modelo_130",
            arguments={
                "trimestre": 1,
                "ingresos_acumulados": 10000,
                "gastos_acumulados": 2000,
                "retenciones_acumuladas": 600,
            },
        )
    )
    assert not pago.is_error and "Modelo 130" in pago.content
    # 21 % of 10.000 = 2.100, less 500 of input VAT.
    iva = toolbox.execute(
        ToolCall(
            id="3",
            name="calcular_modelo_303",
            arguments={"trimestre": 1, "base_tipo_general": 10000, "cuotas_soportadas_deducibles": 500},
        )
    )
    assert not iva.is_error and "resultado = 1600" in iva.content
    assert len(ledger) == 3
    mal = toolbox.execute(ToolCall(id="4", name="calcular_modelo_303", arguments={"trimestre": 9}))
    assert mal.is_error and "trimestre" in mal.content
