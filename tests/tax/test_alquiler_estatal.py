"""State deduction for renting the main home (art. 68.6 LIRPF, RDL 29/2026)."""

from decimal import Decimal

from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import load_params

LINEA = "Deducción estatal por alquiler de vivienda habitual"


def _deduccion(ejercicio: int = 2026, **kw: object) -> Decimal:
    result = calcular_irpf(IRPFInput(ejercicio=ejercicio, **kw))
    return sum((-line.importe for line in result.lineas if line.concepto == LINEA), Decimal(0))


def _base_imponible(**kw: object) -> Decimal:
    result = calcular_irpf(IRPFInput(ejercicio=2026, **kw))
    return result.base_imponible_general + result.base_imponible_ahorro


def test_parameters_match_the_law() -> None:
    av = load_params(2026).alquiler_vivienda_habitual
    assert av is not None
    assert (av.porcentaje, av.base_maxima) == (Decimal("0.10"), Decimal("11630"))
    assert (av.umbral_inferior, av.umbral_superior) == (Decimal("23007.20"), Decimal("33007.20"))
    # The maximum base reaches zero exactly at the upper threshold.
    assert av.base_maxima - av.pendiente * (av.umbral_superior - av.umbral_inferior) == 0


def test_not_available_in_2025() -> None:
    assert load_params(2025).alquiler_vivienda_habitual is None
    assert _deduccion(2025, rendimiento_trabajo_bruto=Decimal("20000"), alquiler_vivienda_habitual=Decimal("9000")) == 0


def test_ten_percent_of_rent_below_lower_threshold() -> None:
    kw = {"rendimiento_trabajo_bruto": Decimal("20000"), "alquiler_vivienda_habitual": Decimal("9000")}
    assert _base_imponible(**kw) <= Decimal("23007.20")
    assert _deduccion(**kw) == Decimal("900.00")


def test_rent_capped_at_maximum_base() -> None:
    assert _deduccion(
        rendimiento_trabajo_bruto=Decimal("20000"), alquiler_vivienda_habitual=Decimal("15000")
    ) == Decimal("1163.00")


def test_maximum_base_shrinks_between_thresholds() -> None:
    kw = {"rendimiento_trabajo_bruto": Decimal("32000"), "alquiler_vivienda_habitual": Decimal("12000")}
    base = _base_imponible(**kw)
    assert Decimal("23007.20") < base < Decimal("33007.20")
    esperado = ((Decimal("11630") - Decimal("1.163") * (base - Decimal("23007.20"))) * Decimal("0.10")).quantize(
        Decimal("0.01")
    )
    assert abs(_deduccion(**kw) - esperado) <= Decimal("0.01")
    assert 0 < _deduccion(**kw) < Decimal("1163")


def test_no_deduction_at_or_above_upper_threshold() -> None:
    kw = {"rendimiento_trabajo_bruto": Decimal("45000"), "alquiler_vivienda_habitual": Decimal("12000")}
    assert _base_imponible(**kw) >= Decimal("33007.20")
    assert _deduccion(**kw) == 0


def test_borne_by_the_state_tax_only() -> None:
    kw = {"rendimiento_trabajo_bruto": Decimal("22000")}
    sin = calcular_irpf(IRPFInput(ejercicio=2026, **kw))
    con = calcular_irpf(IRPFInput(ejercicio=2026, alquiler_vivienda_habitual=Decimal("9000"), **kw))
    assert con.cuota_liquida < sin.cuota_liquida
    assert any("pendiente de convalidación" in aviso for aviso in con.avisos)


def test_catalogue_lists_it() -> None:
    inp = IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=Decimal("20000"))
    por_confirmar = {b.id: b for b in evaluar_beneficios(inp, conocidos={"rendimiento_trabajo_bruto"})}
    assert por_confirmar["alquiler_estatal"].estado == "por_confirmar"
    con = inp.model_copy(update={"alquiler_vivienda_habitual": Decimal("9000")})
    aplicado = {
        b.id: b for b in evaluar_beneficios(con, conocidos={"rendimiento_trabajo_bruto", "alquiler_vivienda_habitual"})
    }
    assert aplicado["alquiler_estatal"].estado == "aplicado"


def test_ceuta_melilla_and_la_palma_residents_are_warned() -> None:
    for lugar in ("Ceuta", "Melilla", "La Palma"):
        r = calcular_irpf(
            IRPFInput(ejercicio=2026, comunidad_autonoma=lugar, rendimiento_trabajo_bruto=Decimal("30000"))
        )
        assert any("art. 68.4" in aviso for aviso in r.avisos), lugar
    otros = calcular_irpf(
        IRPFInput(ejercicio=2026, comunidad_autonoma="Las Palmas", rendimiento_trabajo_bruto=Decimal("30000"))
    )
    assert not any("art. 68.4" in aviso for aviso in otros.avisos)


def test_reviews_are_recorded() -> None:
    revisadas = {r["boe_id"] for r in load_params(2026).revisiones}
    assert {"BOE-A-2026-20823", "BOE-A-2026-18429", "BOE-A-2026-18828", "BOE-A-2026-23602"} <= revisadas
