"""IRPF calculator tests. Expected figures are worked by hand from the scales
and amounts in arts. 19, 20, 57-60, 63, 66, 76, 84 and DA 61 LIRPF, as loaded
in ``hacienda_gpt/tax/data/irpf_2026.json``."""

from decimal import Decimal

import pytest

from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import EjercicioNoDisponibleError, ejercicios_disponibles, load_params

D = Decimal


def _calc(**kwargs):
    return calcular_irpf(IRPFInput(ejercicio=2026, **kwargs))


def _linea(result, prefix: str) -> D:
    return next(line.importe for line in result.lineas if line.concepto.startswith(prefix))


def test_params_load_with_sources_and_scale_matches_the_laws_cumulative_column() -> None:
    assert ejercicios_disponibles() == [2025, 2026]
    params = load_params(2026)
    assert params.fuente_principal.boe_id == "BOE-A-2006-20764"
    escala = params.escala_general_estatal
    # Art. 63.1 prints the cumulative tax at each bracket start; the bracket
    # rates must reproduce that column exactly.
    assert escala.cuota(D("12450")) == D("1182.75")
    assert escala.cuota(D("20200")) == D("2112.75")
    assert escala.cuota(D("35200")) == D("4362.75")
    assert escala.cuota(D("60000")) == D("8950.75")
    assert escala.cuota(D("300000")) == D("62950.75")
    assert escala.cuota(D("400000")) == D("62950.75") + D("24500")
    ahorro = params.escala_ahorro_estatal  # art. 66.1 cumulative column
    assert [ahorro.cuota(D(x)) for x in ("6000", "50000", "200000", "300000")] == [
        D("570"),
        D("5190"),
        D("22440"),
        D("35940"),
    ]
    assert escala.cuota(D(0)) == 0
    with pytest.raises(EjercicioNoDisponibleError, match="2019"):
        load_params(2019)


def test_mid_salary_single_taxpayer() -> None:
    # 30.000 − 1.905 SS − 2.000 = 26.095 (above the art. 20 threshold).
    # Scale(26.095) = 2.112,75 + 5.895 × 15 % = 2.997,00; scale(5.550) = 527,25.
    r = _calc(rendimiento_trabajo_bruto=D(30000), cotizaciones_seguridad_social=D(1905), retenciones=D(4500))
    assert r.base_liquidable_general == D("26095.00")
    assert r.minimo_personal_familiar == D("5550.00")
    assert r.cuota_integra_estatal == D("2469.75")
    assert r.cuota_integra_autonomica == D("2469.75")  # approximated with the state scale…
    assert any("aproximado con una escala igual a la estatal" in aviso for aviso in r.avisos)  # …and says so
    assert r.cuota_liquida == D("4939.50")
    assert r.resultado == D("439.50")
    assert r.tipo_medio_efectivo == D("18.93")
    assert r.parametros_no_verificados == []


def test_low_salary_gets_work_reduction_and_work_deduction_capped_at_the_tax() -> None:
    # Net before "otros gastos": 16.000 − 1.016 = 14.984 → art. 20 b):
    # 7.302 − 1,75 × (14.984 − 14.852) = 7.071. Net: 14.984 − 2.000 − 7.071 = 5.913.
    r = _calc(rendimiento_trabajo_bruto=D(16000), cotizaciones_seguridad_social=D(1016))
    assert _linea(r, "Reducción por obtención de rendimientos del trabajo") == D("-7071.00")
    assert r.base_liquidable_general == D("5913.00")
    assert r.cuota_integra_estatal == D("34.49")  # 561,74 − 527,25
    # DA 61: 590,89 available, but it cannot exceed the tax itself.
    assert _linea(r, "Deducción por obtención de rendimientos del trabajo") == D("-68.98")
    assert r.cuota_liquida == 0 and r.resultado == 0


def test_work_reduction_slopes_and_other_income_limit() -> None:
    def reduccion(bruto: str, **extra) -> D:
        r = _calc(rendimiento_trabajo_bruto=D(bruto), **extra)
        return next((-line.importe for line in r.lineas if line.concepto.startswith("Reducción por obtención")), D(0))

    assert reduccion("14852") == D("7302.00")
    assert reduccion("17673.52") == D("2364.34")  # both formulas meet at the knee
    assert reduccion("18673.52") == D("1224.34")  # 2.364,34 − 1,14 × 1.000
    assert reduccion("19747.5") == 0
    assert reduccion("14852", ganancias_patrimoniales=D(7000)) == 0  # other income above 6.500 €


def test_family_minimums_and_shared_descendants() -> None:
    r = _calc(
        rendimiento_trabajo_bruto=D(60000),
        edad=68,
        num_descendientes=3,
        num_descendientes_menores_3=1,
        num_ascendientes_mayores_65=2,
        num_ascendientes_mayores_75=1,
        grado_discapacidad=33,
    )
    # 5.550 + 1.150 | 2.400 + 2.700 + 4.000 + 2.800 | 2 × 1.150 + 1.400 | 3.000
    assert r.minimo_personal_familiar == D("6700") + D("11900") + D("3700") + D("3000")

    shared = _calc(rendimiento_trabajo_bruto=D(60000), num_descendientes=2, descendientes_compartidos=True)
    assert shared.minimo_personal_familiar == D("5550") + D("2550")
    severe = _calc(rendimiento_trabajo_bruto=D(60000), grado_discapacidad=65, edad=80)
    assert severe.minimo_personal_familiar == D("5550") + D("1150") + D("1400") + D("9000") + D("3000")


def test_savings_base_is_taxed_on_its_own_scale_and_unused_minimum_spills_into_it() -> None:
    r = _calc(rendimiento_capital_mobiliario=D(10000))
    # No general base: the whole 5.550 minimum applies to savings.
    # State: (570 + 4.000 × 10,5 %) − 5.550 × 9,5 % = 990 − 527,25 = 462,75; regional scale is identical.
    assert r.base_liquidable_general == 0 and r.base_liquidable_ahorro == D("10000.00")
    assert r.cuota_integra_estatal == D("462.75") and r.cuota_integra_autonomica == D("462.75")

    negative = _calc(rendimiento_trabajo_bruto=D(40000), ganancias_patrimoniales=D(-3000))
    assert negative.base_liquidable_ahorro == 0
    assert any("art. 49" in aviso for aviso in negative.avisos)


def test_joint_filing_reduction_and_pension_plan_limits() -> None:
    base = _calc(rendimiento_trabajo_bruto=D(50000), cotizaciones_seguridad_social=D(3175))
    joint = _calc(
        rendimiento_trabajo_bruto=D(50000), cotizaciones_seguridad_social=D(3175), tributacion="conjunta_biparental"
    )
    assert base.base_liquidable_general - joint.base_liquidable_general == D("3400")
    mono = _calc(rendimiento_trabajo_bruto=D(50000), tributacion="conjunta_monoparental")
    assert _linea(mono, "Reducción por tributación conjunta") == D("-2150.00")

    # Individual contributions are capped at 1.500 €; the saving is 1.500 × 37 % (18,5 % × 2).
    plan = _calc(
        rendimiento_trabajo_bruto=D(50000), cotizaciones_seguridad_social=D(3175), aportacion_plan_pensiones=D(5000)
    )
    assert _linea(plan, "Reducción por aportaciones") == D("-1500.00")
    assert base.resultado - plan.resultado == D("555.00")
    assert any("Solo se han reducido 1500 €" in aviso for aviso in plan.avisos)

    employer = _calc(rendimiento_trabajo_bruto=D(50000), contribucion_empresa_plan_pensiones=D(9000))
    assert _linea(employer, "Reducción por aportaciones") == D("-8500.00")
    # The 30 % limit binds for a small earner: 30 % × 4.000 = 1.200 < 1.500.
    small = _calc(rendimiento_actividad_neto=D(4000), aportacion_plan_pensiones=D(1500))
    assert _linea(small, "Reducción por aportaciones") == D("-1200.00")
    assert any("cinco ejercicios siguientes" in aviso for aviso in small.avisos)
    # And the reduction can never push the base below zero.
    wiped = _calc(rendimiento_trabajo_bruto=D(4000), aportacion_plan_pensiones=D(1500))
    assert _linea(wiped, "Reducción por aportaciones") == 0 and wiped.base_liquidable_general == 0


def test_deductions_donations_housing_maternity_and_withholdings() -> None:
    r = _calc(
        rendimiento_trabajo_bruto=D(40000),
        cotizaciones_seguridad_social=D(2540),
        donativos=D(400),
        pagos_vivienda_habitual_pre2013=D(12000),
        hijos_deduccion_maternidad=1,
        gastos_guarderia=D(3000),
        num_descendientes=1,
        num_descendientes_menores_3=1,
        retenciones=D(7000),
    )
    assert _linea(r, "Deducción por donativos") == D("-260.00")  # 250 × 80 % + 150 × 40 %
    assert _linea(r, "Deducción por inversión en vivienda") == D("-1356.00")  # 9.040 × 15 %
    assert _linea(r, "Deducción por maternidad") == D("-2200.00")  # 1.200 + min(3.000, 1.000)
    # The housing rate comes from the 2012 wording, which was not re-checked: flagged, not hidden.
    assert len(r.parametros_no_verificados) == 1 and "transitoria 18" in r.parametros_no_verificados[0]
    assert r.resultado == r.cuota_liquida - D(7000) - D(2200)

    recurring = _calc(rendimiento_trabajo_bruto=D(40000), donativos=D(400), donativos_recurrentes=True)
    assert _linea(recurring, "Deducción por donativos") == D("-267.50")  # 200 + 150 × 45 %
    capped = _calc(rendimiento_trabajo_bruto=D(12000), donativos=D(5000))
    assert any("10 %" in aviso for aviso in capped.avisos)

    # Maternity is refundable: it produces a refund even with no tax due.
    refund = _calc(rendimiento_trabajo_bruto=D(10000), hijos_deduccion_maternidad=1)
    assert refund.cuota_liquida == 0 and refund.resultado == D("-1200.00")


def test_regional_scale_is_used_when_loaded() -> None:
    params = load_params(2026)
    flat = params.escala_general_estatal.model_copy(
        update={"tramos": [params.escala_general_estatal.tramos[-1].model_copy(update={"tipo": D("0.10")})]}
    )
    custom = params.model_copy(update={"escalas_generales_autonomicas": {"testlandia": flat}})
    r = calcular_irpf(
        IRPFInput(ejercicio=2026, comunidad_autonoma="Testlandia", rendimiento_trabajo_bruto=D(32000)), custom
    )
    assert r.cuota_integra_autonomica == D("2445.00")  # (30.000 − 5.550) × 10 %
    assert not any("aproximado" in aviso for aviso in r.avisos)
    assert any("no los mínimos ni las deducciones propios" in aviso for aviso in r.avisos)


def test_2025_omits_the_unverifiable_work_deduction_and_says_so() -> None:
    r = calcular_irpf(
        IRPFInput(ejercicio=2025, rendimiento_trabajo_bruto=D(16000), cotizaciones_seguridad_social=D(1016))
    )
    assert not any(line.concepto.startswith("Deducción por obtención") for line in r.lineas)
    assert r.cuota_liquida == D("68.98")
    assert any("disposición adicional 61" in aviso for aviso in r.avisos)


def test_no_income_and_negative_general_base_do_not_crash() -> None:
    assert _calc().resultado == 0
    loss = _calc(rendimiento_actividad_neto=D(-5000))
    assert loss.base_liquidable_general == 0 and any("negativa" in aviso for aviso in loss.avisos)
