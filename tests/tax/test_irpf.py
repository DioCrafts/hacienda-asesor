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


def _cents(value: D) -> D:
    return value.quantize(D("0.01"))


def _linea(result, prefix: str) -> D:
    return next(line.importe for line in result.lineas if line.concepto.startswith(prefix))


def test_params_load_with_sources_and_scale_matches_the_laws_cumulative_column() -> None:
    assert ejercicios_disponibles() == [2025, 2026]
    assert "madrid" in load_params(2026).comunidades
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


def test_madrid_scale_matches_the_laws_cumulative_column() -> None:
    madrid = load_params(2026).comunidad("Comunidad de Madrid")
    assert madrid is not None and madrid.fuente_principal.boe_id == "BOCM-m-2010-90068"
    escala = madrid.escala_general
    # Art. 1 DLeg. 1/2010 prints the cumulative tax at each bracket start.
    assert [_cents(escala.cuota(D(x))) for x in ("13362.22", "19004.63", "35425.68", "57320.40")] == [
        D("1135.79"),
        D("1739.53"),
        D("3841.42"),
        D("7651.10"),
    ]
    assert load_params(2026).comunidad(" MADRID ") is madrid
    assert load_params(2026).comunidad("Navarra") is None and load_params(2026).comunidad(None) is None


def test_madrid_resident_uses_regional_scale_and_minimums() -> None:
    # General base 26.095 (see test_mid_salary_single_taxpayer).
    # Regional: 1.739,53 + (26.095 − 19.004,63) × 12,8 % = 2.647,10; minimum 5.956,65 × 8,5 % = 506,32.
    r = _calc(
        comunidad_autonoma="Madrid",
        rendimiento_trabajo_bruto=D(30000),
        cotizaciones_seguridad_social=D(1905),
        retenciones=D(4500),
    )
    assert r.comunidad_aplicada == "Comunidad de Madrid"
    assert r.cuota_integra_estatal == D("2469.75")  # unchanged
    assert r.minimo_personal_familiar == D("5550.00") and r.minimo_autonomico == D("5956.65")
    assert r.cuota_integra_autonomica == D("2140.78")
    assert r.cuota_liquida == D("4610.53") and r.resultado == D("110.53")
    assert not any("aproximado" in aviso for aviso in r.avisos)
    assert any("Se aplican la escala y los mínimos de Comunidad de Madrid" in aviso for aviso in r.avisos)
    assert "BOCM-m-2010-90068" in r.fuente and r.parametros_no_verificados == []

    # Regional family minimums: 5.956,65 + 2.575,85 + 2.897,83 + 3.005,16 (one child under three).
    family = _calc(
        comunidad_autonoma="madrid",
        rendimiento_trabajo_bruto=D(60000),
        num_descendientes=2,
        num_descendientes_menores_3=1,
    )
    assert family.minimo_autonomico == D("14435.49")
    assert family.minimo_personal_familiar == D("13450.00")  # state: 5.550 + 2.400 + 2.700 + 2.800


def test_unloaded_community_is_still_approximated_and_flagged() -> None:
    r = _calc(comunidad_autonoma="Navarra", rendimiento_trabajo_bruto=D(30000), cotizaciones_seguridad_social=D(1905))
    assert r.comunidad_aplicada is None and r.cuota_integra_autonomica == r.cuota_integra_estatal
    assert any("«Navarra» no está cargada" in aviso for aviso in r.avisos)


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


# --------------------------------------------------------------------------- #
# Comunidad de Madrid deductions (DLeg. 1/2010, arts. 4, 8, 9, 11, 11 bis, 14, 18)
# --------------------------------------------------------------------------- #


def _madrid(**kwargs):
    return _calc(comunidad_autonoma="Madrid", **kwargs)


def _aplicada(result, prefix: str) -> D:
    return next((-line.importe for line in result.lineas if line.concepto.startswith(prefix)), D(0))


def _no_aplicada(result, nombre: str) -> str:
    return next((a for a in result.avisos if a.startswith(f"Deducción madrileña por {nombre} no aplicada")), "")


def test_madrid_rent_deduction_for_under_40s() -> None:
    # Base 24.000 − 1.524 − 2.000 = 20.476 (≤ 26.414,22). Rent 9.600 > 20 % of the base.
    # 30 % × 9.600 = 2.880 → capped at 1.237,20.
    # Regional: 1.739,53 + 1.471,37 × 12,8 % = 1.927,87; − 506,32 = 1.421,55; − 1.237,20 = 184,35.
    # State: 2.112,75 + 276 × 15 % − 527,25 = 1.626,90.
    base = dict(
        rendimiento_trabajo_bruto=D(24000), cotizaciones_seguridad_social=D(1524), alquiler_vivienda_habitual=D(9600)
    )
    r = _madrid(edad=30, **base)
    assert _aplicada(r, "Deducción autonómica por alquiler") == D("1237.20")
    assert r.cuota_integra_autonomica == D("1421.55") and r.cuota_integra_estatal == D("1626.90")
    # Since RDL 29/2026 the state rent deduction (art. 68.6) also applies: 10 % × 9.600 = 960.
    assert _aplicada(r, "Deducción estatal por alquiler") == D("960.00")
    assert r.cuota_liquida == D("851.25")

    assert "menores de 40" in _no_aplicada(_madrid(edad=40, **base), "alquiler")
    assert "falta tu edad" in _no_aplicada(_madrid(**base), "alquiler")
    cheap = _madrid(edad=30, **{**base, "alquiler_vivienda_habitual": D(4000)})
    assert "no supera el 20 %" in _no_aplicada(cheap, "alquiler")
    rich = _madrid(edad=30, **{**base, "rendimiento_trabajo_bruto": D(40000)})
    assert "supera 26414.22 €" in _no_aplicada(rich, "alquiler")
    household = _madrid(edad=30, base_imponible_resto_unidad_familiar=D(45000), **base)
    assert "unidad familiar" in _no_aplicada(household, "alquiler")
    for failed in (cheap, rich, household):
        assert _aplicada(failed, "Deducción autonómica por alquiler") == 0


def test_madrid_birth_deduction_and_sharing_between_parents() -> None:
    family = dict(
        rendimiento_trabajo_bruto=D(24000),
        cotizaciones_seguridad_social=D(1524),
        num_descendientes=1,
        num_descendientes_menores_3=1,
        hijos_nacidos_ultimos_3_ejercicios=1,
    )
    assert _aplicada(_madrid(**family), "Deducción autonómica por nacimiento") == D("721.70")
    shared = _madrid(descendientes_compartidos=True, **family)
    assert _aplicada(shared, "Deducción autonómica por nacimiento") == D("360.85")
    high = _madrid(**{**family, "rendimiento_trabajo_bruto": D(40000)})
    assert "supera 30930.00 €" in _no_aplicada(high, "nacimiento o adopción")


def test_madrid_education_expenses_rates_and_caps() -> None:
    kids = dict(rendimiento_trabajo_bruto=D(60000), cotizaciones_seguridad_social=D(3810), num_descendientes=2)
    # 15 % × (6.000 + 1.000) + 5 % × 400 = 1.070. The cap is per child on that child's own expenses, and
    # how they are split between the two children is not known: a single cap of 927,90 is granted.
    full = _madrid(
        gastos_escolaridad=D(6000), gastos_ensenanza_idiomas=D(1000), gastos_uniformes_escolares=D(400), **kids
    )
    assert _aplicada(full, "Deducción autonómica por gastos educativos") == D("927.90")
    assert any("como si todos los gastos fueran de un solo hijo" in aviso for aviso in full.avisos)
    assert any("deducidos de tus datos" in aviso for aviso in full.avisos)  # family size was inferred
    # A single child with those expenses: the cap is not reached.
    solo = _madrid(
        gastos_escolaridad=D(6000), gastos_ensenanza_idiomas=D(1000), gastos_uniformes_escolares=D(400),
        **{**kids, "num_descendientes": 1},
    )  # fmt: skip
    assert _aplicada(solo, "Deducción autonómica por gastos educativos") == D("927.90")
    assert not any("un solo hijo" in aviso for aviso in solo.avisos)

    # Without schooling fees the cap is 412,40 per child: 15 % × 4.000 = 600 → 412,40.
    one = {**kids, "num_descendientes": 1}
    languages = _madrid(gastos_ensenanza_idiomas=D(4000), miembros_unidad_familiar=3, **one)
    assert _aplicada(languages, "Deducción autonómica por gastos educativos") == D("412.40")
    assert not any("deducidos de tus datos" in aviso for aviso in languages.avisos)
    # First cycle of infant education: 15 % × 9.000 = 1.350 → 1.031.
    nursery = _madrid(gastos_escolaridad=D(9000), num_hijos_primer_ciclo_infantil=1, **one)
    assert _aplicada(nursery, "Deducción autonómica por gastos educativos") == D("1031.00")
    # Shared between parents filing separately.
    halved = _madrid(gastos_escolaridad=D(6000), descendientes_compartidos=True, **kids)
    assert _aplicada(halved, "Deducción autonómica por gastos educativos") == D("450.00")

    # Family income ceiling: 30.930 € per member of the family unit.
    over = _madrid(gastos_escolaridad=D(6000), miembros_unidad_familiar=1, **kids)
    assert "30.930 € × 1 miembros" in _no_aplicada(over, "gastos educativos")
    childless = _madrid(rendimiento_trabajo_bruto=D(30000), gastos_escolaridad=D(6000))
    assert "no constan hijos" in _no_aplicada(childless, "gastos educativos")


def test_madrid_education_expenses_child_by_child() -> None:
    """Art. 11.4: 412,40 € per child, 927,90 € for a child with schooling fees, 1.031 € in the first
    cycle of infant education (where only the schooling counts) — each on that child's own expenses."""
    base = dict(rendimiento_trabajo_bruto=D(60000), cotizaciones_seguridad_social=D(3810))

    def con(*hijos: dict[str, object]) -> D:
        return _aplicada(_madrid(descendientes=list(hijos), **base), "Deducción autonómica por gastos educativos")

    # The audit's case: 8.000 € of schooling of ONE of two children → 927,90 (the old code gave 1.200).
    assert con({"edad": 10, "gastos": {"gastos_escolaridad": 8000}}, {"edad": 7}) == D("927.90")
    # One child with fees (capped at 927,90) and one with languages only (15 % × 4.000 = 600 → 412,40):
    # the higher cap is not extended to the child without schooling fees.
    assert con(
        {"edad": 10, "gastos": {"gastos_escolaridad": 8000}},
        {"edad": 7, "gastos": {"gastos_ensenanza_idiomas": 4000}},
    ) == D("1340.30")
    # Below the caps nothing is lost: 15 % × 3.000 + (15 % × 1.000 + 5 % × 400) = 450 + 170.
    assert con(
        {"edad": 10, "gastos": {"gastos_escolaridad": 3000}},
        {"edad": 7, "gastos": {"gastos_ensenanza_idiomas": 1000, "gastos_uniformes_escolares": 400}},
    ) == D("620.00")
    # First cycle of infant education: schooling only (languages and uniforms are left out), cap 1.031.
    assert con(
        {"edad": 2, "etapa_educativa": "infantil_primer_ciclo",
         "gastos": {"gastos_escolaridad": 2000, "gastos_ensenanza_idiomas": 1000, "gastos_uniformes_escolares": 300}},
    ) == D("300.00")  # fmt: skip
    assert con(
        {"edad": 2, "etapa_educativa": "infantil_primer_ciclo", "gastos": {"gastos_escolaridad": 9000}},
        {"edad": 8, "gastos": {"gastos_escolaridad": 9000}},
    ) == D("1958.90")  # 1.031 + 927,90
    exacto = _madrid(descendientes=[{"edad": 10, "gastos": {"gastos_escolaridad": 8000}}, {"edad": 7}], **base)
    assert any("hijo a hijo con el desglose" in aviso for aviso in exacto.avisos)
    assert exacto.base_imponible_general == _madrid(num_descendientes=2, **base).base_imponible_general
    # The breakdown must add up to the total when both are given.
    with pytest.raises(ValueError, match="no coincide con el total"):
        _madrid(gastos_escolaridad=D(5000), descendientes=[{"edad": 10, "gastos": {"gastos_escolaridad": 8000}}])


def test_madrid_low_income_deduction_is_computed_after_the_data_driven_ones() -> None:
    # Art. 14: 10 % of the regional tax left after the other regional deductions, the fostering one included.
    base = dict(rendimiento_trabajo_bruto=D(22000), cotizaciones_seguridad_social=D(1397), num_descendientes=2)
    sin, con = _madrid(**base), _madrid(menores_acogidos=1, **base)
    familias = "Deducción autonómica para familias con dos o más descendientes"
    acogimiento = _aplicada(con, "Deducción autonómica: Acogimiento familiar de menores")
    assert acogimiento > 0
    assert _aplicada(sin, familias) - _aplicada(con, familias) == (acogimiento * D("0.1")).quantize(D("0.01"))


def test_madrid_care_and_local_donations_deductions() -> None:
    base = dict(rendimiento_trabajo_bruto=D(40000), cotizaciones_seguridad_social=D(2540), num_descendientes=1)
    toddler = {**base, "num_descendientes_menores_3": 1}
    # 25 % × 2.000 = 500 → 463,95; large families: 40 % × 2.000 = 800 → 618,60.
    assert _aplicada(_madrid(cotizaciones_empleada_hogar=D(2000), **toddler), "Deducción autonómica por cuidado") == D(
        "463.95"
    )
    numerous = _madrid(cotizaciones_empleada_hogar=D(2000), familia_numerosa=True, **toddler)
    assert _aplicada(numerous, "Deducción autonómica por cuidado") == D("618.60")
    assert _aplicada(_madrid(cotizaciones_empleada_hogar=D(1000), **toddler), "Deducción autonómica por cuidado") == D(
        "250.00"
    )
    assert "menor de tres años" in _no_aplicada(
        _madrid(cotizaciones_empleada_hogar=D(2000), **base), "cuidado de menores o dependientes"
    )

    donor = _madrid(donativos_fundaciones_madrid=D(1000), **base)
    assert _aplicada(donor, "Deducción autonómica por donativos") == D("150.00")


def test_madrid_low_income_family_deduction_is_ten_percent_of_what_remains() -> None:
    # Base 22.000 − 1.397 − 2.000 = 18.603 ≤ 24.744, two descendants.
    r = _madrid(rendimiento_trabajo_bruto=D(22000), cotizaciones_seguridad_social=D(1397), num_descendientes=2)
    assert r.minimo_autonomico == D("11430.33")
    assert r.cuota_integra_autonomica == D("724.97")  # 1.696,55 − 971,58
    assert _aplicada(r, "Deducción autonómica para familias") == D("72.50")
    # The other parent's income counts towards the 24.744 € ceiling.
    both = _madrid(
        rendimiento_trabajo_bruto=D(22000),
        cotizaciones_seguridad_social=D(1397),
        num_descendientes=2,
        base_imponible_resto_unidad_familiar=D(20000),
    )
    assert _aplicada(both, "Deducción autonómica para familias") == 0


def test_regional_deductions_cannot_push_the_regional_tax_below_zero() -> None:
    r = _madrid(
        rendimiento_trabajo_bruto=D(21000),
        cotizaciones_seguridad_social=D(1333),
        edad=30,
        alquiler_vivienda_habitual=D(9600),
        num_descendientes=1,
        hijos_nacidos_ultimos_3_ejercicios=1,
    )
    # Rent (1.237,20) + birth (721,70) exceed the regional tax: it drops to zero
    # and the state tax only loses the state rent deduction (art. 68.6: 960).
    assert _aplicada(r, "Deducción autonómica por alquiler") == D("1237.20")
    assert _aplicada(r, "Deducción autonómica por nacimiento") == D("721.70")
    assert r.cuota_integra_autonomica < D("1958.90")
    assert any("art. 77.2" in aviso for aviso in r.avisos)
    assert r.cuota_liquida == r.cuota_integra_estatal - D("960.00")


def test_general_deductions_are_split_between_state_and_regional_tax() -> None:
    # A 200 € donation deduction is borne 100 € by each tax (arts. 67.1.b and 77.1.a).
    # With only 34,49 € of tax on each side, each side can absorb just its own tax.
    low = dict(rendimiento_trabajo_bruto=D(16000), cotizaciones_seguridad_social=D(1016), donativos=D(250))
    r = calcular_irpf(IRPFInput(ejercicio=2025, **low))
    assert r.cuota_integra_estatal == D("34.49") and r.cuota_liquida == 0

    # Madrid inputs supplied for a non-Madrid simulation are reported, not silently ignored.
    elsewhere = _calc(
        comunidad_autonoma="Navarra", rendimiento_trabajo_bruto=D(24000), edad=30, alquiler_vivienda_habitual=D(9600)
    )
    assert any("no tiene deducciones calculables para tu comunidad" in aviso for aviso in elsewhere.avisos)
    assert _aplicada(elsewhere, "Deducción autonómica por alquiler") == 0
