"""Impuesto sobre Sociedades for small companies: hand-computed cases.

Every expected figure is worked out in the comment next to it from the rates
stored in ``sociedades_2026.json`` (read from the Ley 27/2014 on boe.es).
"""

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import re

import pytest

from hacienda_gpt.tax.autonomos_params import textos_de_directorio
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import EjercicioNoDisponibleError
from hacienda_gpt.tax.sociedades import (
    ComparacionInput,
    PagoFraccionadoSociedadesInput,
    SociedadesInput,
    calcular_pago_fraccionado_sociedades,
    calcular_sociedades,
    comparar_autonomo_vs_sociedad,
)
from hacienda_gpt.tax.sociedades_params import (
    SociedadesParams,
    cifra_a_decimal,
    comprobar_evidencias,
    ejercicios_sociedades_disponibles,
    load_sociedades_params,
)

D = Decimal
EXTRACTOS = Path(__file__).resolve().parents[1] / "fixtures" / "boe_sociedades"


def _is(**kwargs):
    return calcular_sociedades(SociedadesInput(**{"ejercicio": 2026, **kwargs}))


def _pf(**kwargs):
    return calcular_pago_fraccionado_sociedades(PagoFraccionadoSociedadesInput(**{"ejercicio": 2026, **kwargs}))


def _aviso(result, fragmento: str) -> bool:
    return any(fragmento in aviso for aviso in result.avisos)


# --- Data and its evidence ---------------------------------------------------


def test_parameters_are_the_ones_read_from_the_law() -> None:
    p = load_sociedades_params(2026)
    assert ejercicios_sociedades_disponibles() == [2025, 2026, 2027, 2028]
    assert p.tipos_gravamen.general == D("0.25")
    assert p.tipos_gravamen.nueva_creacion == D("0.15")
    # Transitional provision 44: (first bracket, rest, reduced-size) by year of start.
    assert [
        (year, t.micro_primer_tramo, t.micro_resto, t.reducida_dimension)
        for year, t in sorted(p.tipos_gravamen.por_ano.items())
    ] == [
        ("2025", D("0.21"), D("0.22"), D("0.24")),
        ("2026", D("0.19"), D("0.21"), D("0.23")),
        ("2027", D("0.17"), D("0.20"), D("0.22")),
        ("2028", D("0.17"), D("0.20"), D("0.21")),
    ]
    assert p.pago_fraccionado.porcentaje_cuota == D("0.18")
    assert p.compensacion_bases_negativas.limite_porcentaje == D("0.70")
    assert (p.reserva_nivelacion.porcentaje, p.reserva_nivelacion.limite) == (D("0.10"), D(1000000))
    assert p.comparacion_irpf.retencion_capital_mobiliario == D("0.19")
    assert all(g.fuente.verificado for g in p.grupos().values())
    with pytest.raises(EjercicioNoDisponibleError):
        load_sociedades_params(2024)


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    textos = textos_de_directorio(EXTRACTOS)
    p = load_sociedades_params(2026)
    assert set(textos) == set(p.fuentes_principales)
    assert comprobar_evidencias(p, textos) == []


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point HACIENDA_BOE_PAGINAS at a directory with BOE-A-*.html straight from boe.es.
    directorio = os.environ.get("HACIENDA_BOE_PAGINAS")
    if not directorio or not (Path(directorio) / "BOE-A-2014-12328.html").exists():
        pytest.skip("HACIENDA_BOE_PAGINAS no contiene la Ley 27/2014")
    # The annexes of the two orders only exist as PDF: their extracts stay the reference for them.
    textos = {**textos_de_directorio(EXTRACTOS), **textos_de_directorio(Path(directorio))}
    assert comprobar_evidencias(load_sociedades_params(2026), textos) == []


def test_self_check_catches_a_wrong_figure_a_wrong_quote_and_a_figure_without_evidence() -> None:
    p = load_sociedades_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    raw = p.model_dump(mode="json")
    tg = raw["tipos_gravamen"]

    por_ano = {**tg["por_ano"], "2026": {**tg["por_ano"]["2026"], "reducida_dimension": "0.20"}}
    wrong = {**raw, "tipos_gravamen": {**tg, "por_ano": por_ano}}
    assert comprobar_evidencias(SociedadesParams.model_validate(wrong), textos) == [
        "tipos_gravamen.por_ano.2026.reducida_dimension: el dato es 0.20 y la cita dice «23 %»"
    ]

    pf = raw["pago_fraccionado"]
    invented = [{**pf["evidencia"][0], "cita": "El pago fraccionado será del 18 por ciento de la cuota."}]
    bad_quote = {**raw, "pago_fraccionado": {**pf, "evidencia": invented + pf["evidencia"][1:]}}
    assert comprobar_evidencias(SociedadesParams.model_validate(bad_quote), textos) == [
        "pago_fraccionado: la cita de art. 40.2 LIS no aparece literal en BOE-A-2014-12328"
    ]

    orphan = {**raw, "pago_fraccionado": {**pf, "evidencia": pf["evidencia"][1:]}}
    assert comprobar_evidencias(SociedadesParams.model_validate(orphan), textos) == [
        "pago_fraccionado.porcentaje_cuota: cifra sin evidencia"
    ]

    # "5 por ciento" must be found as a figure of its own, not inside "25 por ciento".
    rc = raw["reserva_capitalizacion"]
    limites = [{**rc["evidencia"][2], "cifras": {"limite_base": "5 por ciento"}}]
    inside = {**raw, "reserva_capitalizacion": {**rc, "evidencia": rc["evidencia"][:2] + limites}}
    assert (
        "reserva_capitalizacion.limite_base: «5 por ciento» no aparece en la cita de art. 25.1 LIS (límites)"
        in comprobar_evidencias(SociedadesParams.model_validate(inside))
    )


def test_reading_figures_as_the_law_spells_them() -> None:
    assert cifra_a_decimal("25 por ciento") == D("0.25")
    assert cifra_a_decimal("23 %") == D("0.23")
    assert cifra_a_decimal("26,5 por ciento") == D("0.265")
    assert cifra_a_decimal("50.000 euros") == 50000
    assert cifra_a_decimal("1 millón de euros") == 1_000_000
    assert cifra_a_decimal("60 millones de euros") == 60_000_000
    assert cifra_a_decimal("cinco") == 5
    assert cifra_a_decimal("séptimos") == 7


# --- Rate (art. 29.1 and transitional provision 44) --------------------------


def test_micro_company_scale_2026() -> None:
    # Turnover 300.000 < 1 M. Base 80.000: 50.000 x 19 % = 9.500; 30.000 x 21 % = 6.300; total 15.800.
    r = _is(resultado_contable=D(80000), cifra_negocios_anterior=D(300000))
    assert r.regimen == "microempresa"
    assert (r.tipo, r.tipo_resto) == (D("0.19"), D("0.21"))
    assert r.base_imponible == D("80000.00")
    assert r.cuota_integra == D("15800.00")
    assert r.cuota_diferencial == D("15800.00")
    assert r.tipo_medio == D("19.75")  # 15.800 / 80.000
    assert _aviso(r, "No se comprueba")
    assert _aviso(r, "reserva de nivelación")
    assert r.parametros_no_verificados == []


def test_micro_company_base_inside_the_first_bracket() -> None:
    # 40.000 x 19 % = 7.600.
    assert _is(resultado_contable=D(40000), cifra_negocios_anterior=D(0)).cuota_integra == D("7600.00")


def test_micro_scale_by_year_of_start() -> None:
    # Base 80.000. 2025: 50.000 x 21 % + 30.000 x 22 % = 10.500 + 6.600 = 17.100.
    # 2027 and 2028 (art. 29.1 itself): 50.000 x 17 % + 30.000 x 20 % = 8.500 + 6.000 = 14.500.
    kwargs = {"resultado_contable": D(80000), "cifra_negocios_anterior": D(999999)}
    assert _is(ejercicio=2025, **kwargs).cuota_integra == D("17100.00")
    r27 = _is(ejercicio=2027, **kwargs)
    assert r27.cuota_integra == D("14500.00")
    assert _aviso(r27, "una ley posterior puede cambiarlos")
    assert _is(ejercicio=2028, **kwargs).cuota_integra == D("14500.00")
    with pytest.raises(EjercicioNoDisponibleError):
        _is(ejercicio=2029, **kwargs)


def test_reduced_size_entity_by_year() -> None:
    # Turnover exactly 1 M is no longer "inferior a 1 millón": reduced-size rate on 200.000.
    kwargs = {"resultado_contable": D(200000), "cifra_negocios_anterior": D(1000000)}
    r = _is(**kwargs)
    assert r.regimen == "reducida_dimension"
    assert r.cuota_integra == D("46000.00")  # 23 %
    assert _is(ejercicio=2025, **kwargs).cuota_integra == D("48000.00")  # 24 %
    assert _is(ejercicio=2027, **kwargs).cuota_integra == D("44000.00")  # 22 %
    assert _is(ejercicio=2028, **kwargs).cuota_integra == D("42000.00")  # 21 %


def test_general_rate() -> None:
    # Turnover of exactly 10 M is not "inferior a 10 millones": 25 % of 200.000 = 50.000.
    r = _is(resultado_contable=D(200000), cifra_negocios_anterior=D(10000000))
    assert r.regimen == "general"
    assert r.cuota_integra == D("50000.00")
    assert _aviso(r, "art. 101.4")
    # Unknown turnover: general rate, and the result says which rates could apply instead.
    r = _is(resultado_contable=D(200000))
    assert r.cuota_integra == D("50000.00")
    assert _aviso(r, "No se ha indicado el importe neto de la cifra de negocios")


def test_holding_entity_gets_the_general_rate_even_if_small() -> None:
    r = _is(resultado_contable=D(40000), cifra_negocios_anterior=D(50000), entidad_patrimonial=True)
    assert r.regimen == "entidad_patrimonial"
    assert r.cuota_integra == D("10000.00")  # 25 %
    # ... and even if the user also claims the newly-created rate.
    r = _is(
        resultado_contable=D(40000),
        cifra_negocios_anterior=D(50000),
        entidad_patrimonial=True,
        nueva_creacion_tipo_reducido=True,
    )
    assert r.cuota_integra == D("10000.00")


def test_newly_created_company_rate_and_its_warning() -> None:
    # 15 % of 80.000 = 12.000 (lower than the micro scale, so it prevails).
    r = _is(resultado_contable=D(80000), cifra_negocios_anterior=D(100000), nueva_creacion_tipo_reducido=True)
    assert r.regimen == "nueva_creacion"
    assert r.cuota_integra == D("12000.00")
    assert _aviso(r, "más del 50 % del capital")
    assert _aviso(r, "NO tiene derecho a este tipo")


def test_short_period_prorates_the_first_bracket() -> None:
    # 180 days: first bracket 50.000 x 180 / 365 = 24.657,53.
    # 24.657,53 x 19 % = 4.684,93; (60.000 - 24.657,53) = 35.342,47 x 21 % = 7.421,92; total 12.106,85.
    r = _is(resultado_contable=D(60000), cifra_negocios_anterior=D(100000), dias_periodo=180)
    assert r.cuota_integra == D("12106.85")
    # A leap-year period of 366 days is not "inferior al año": no pro-rating.
    r = _is(resultado_contable=D(60000), cifra_negocios_anterior=D(100000), dias_periodo=366)
    assert r.cuota_integra == D("11600.00")  # 9.500 + 10.000 x 21 %


# --- Base: adjustments, capitalisation reserve, losses ------------------------


def test_adjustments_and_negative_base() -> None:
    # 10.000 + 5.000 - 40.000 = -25.000: no quota; the loss is carried forward with the 8.000 pending.
    r = _is(
        resultado_contable=D(10000),
        ajustes_positivos=D(5000),
        ajustes_negativos=D(40000),
        bases_negativas_pendientes=D(8000),
        cifra_negocios_anterior=D(100000),
        retenciones_ingresos_cuenta=D(300),
    )
    assert r.base_imponible_previa == D("-25000.00")
    assert r.base_imponible == D("-25000.00")
    assert r.bases_negativas_compensadas == 0
    assert r.bases_negativas_pendientes == D("33000.00")
    assert r.cuota_integra == 0
    assert r.cuota_diferencial == D("-300.00")  # refund of the withholdings (art. 41)
    assert r.tipo_medio == 0


def test_capitalisation_reserve_and_losses_micro() -> None:
    # Base previa 80.000. Reserve: 20 % x 20.000 = 4.000; limit 25 % x 80.000 = 20.000 (turnover < 1 M) -> 4.000.
    # Losses: 10.000 pending, limit max(70 % x 80.000, 1 M) -> 10.000. Base = 80.000 - 4.000 - 10.000 = 66.000.
    # Quota: 50.000 x 19 % = 9.500; 16.000 x 21 % = 3.360; total 12.860.
    r = _is(
        resultado_contable=D(80000),
        cifra_negocios_anterior=D(300000),
        incremento_fondos_propios=D(20000),
        bases_negativas_pendientes=D(10000),
    )
    assert r.reduccion_reserva_capitalizacion == D("4000.00")
    assert r.reserva_capitalizacion_pendiente == 0
    assert r.bases_negativas_compensadas == D("10000.00")
    assert r.bases_negativas_pendientes == 0
    assert r.base_imponible == D("66000.00")
    assert r.cuota_integra == D("12860.00")
    assert _aviso(r, "mantener el incremento durante 3 años")


def test_capitalisation_reserve_limit_and_carry_forward() -> None:
    # Turnover 5 M: limit 20 % x 100.000 = 20.000. Right: 20 % x 150.000 = 30.000 + 2.000 pending = 32.000.
    # Applied 20.000, 12.000 left. Base 80.000 x 23 % = 18.400.
    r = _is(
        resultado_contable=D(100000),
        cifra_negocios_anterior=D(5000000),
        incremento_fondos_propios=D(150000),
        reserva_capitalizacion_pendiente=D(2000),
    )
    assert r.reduccion_reserva_capitalizacion == D("20000.00")
    assert r.reserva_capitalizacion_pendiente == D("12000.00")
    assert r.cuota_integra == D("18400.00")
    assert _aviso(r, "2 años siguientes")
    # Same company with turnover under 1 M in the 12 months before: limit 25 % x 100.000 = 25.000.
    r = _is(
        resultado_contable=D(100000),
        cifra_negocios_anterior=D(5000000),
        cifra_negocios_12_meses_anteriores=D(900000),
        incremento_fondos_propios=D(150000),
    )
    assert r.reduccion_reserva_capitalizacion == D("25000.00")


@pytest.mark.parametrize(
    ("plantilla", "reduccion"),
    [
        (None, "2000.00"),  # 20 %
        ("1.99", "2000.00"),  # below the minimum of 2 %
        ("2", "2300.00"),  # 23 %: "un mínimo de un 2 por ciento sin superar un 5 por ciento"
        ("5", "2300.00"),
        ("5.01", "2650.00"),  # 26,5 %: "entre un 5 y un 10 por ciento"
        ("10", "2650.00"),
        ("10.01", "3000.00"),  # 30 %: "superior a un 10 por ciento"
    ],
)
def test_capitalisation_reserve_percentage_by_workforce_increase(plantilla: str | None, reduccion: str) -> None:
    # Increase of equity 10.000; base 100.000 so the limit (20.000) does not bind.
    r = _is(
        resultado_contable=D(100000),
        cifra_negocios_anterior=D(2000000),
        incremento_fondos_propios=D(10000),
        incremento_plantilla_porcentaje=None if plantilla is None else D(plantilla),
    )
    assert r.reduccion_reserva_capitalizacion == D(reduccion)


def test_losses_limit_seventy_percent_above_one_million() -> None:
    # Base previa 2.000.000, losses pending 1.800.000, turnover 8 M.
    # Limit: max(70 % x 2.000.000 = 1.400.000, 1.000.000) = 1.400.000. Base 600.000 x 23 % = 138.000.
    r = _is(
        resultado_contable=D(2000000),
        cifra_negocios_anterior=D(8000000),
        bases_negativas_pendientes=D(1800000),
    )
    assert r.bases_negativas_compensadas == D("1400000.00")
    assert r.bases_negativas_pendientes == D("400000.00")
    assert r.cuota_integra == D("138000.00")


def test_losses_one_million_always_available() -> None:
    # Base previa 1.200.000: 70 % = 840.000 < 1.000.000 -> 1.000.000 compensated. Base 200.000 x 23 % = 46.000.
    r = _is(
        resultado_contable=D(1200000),
        cifra_negocios_anterior=D(8000000),
        bases_negativas_pendientes=D(1800000),
    )
    assert r.bases_negativas_compensadas == D("1000000.00")
    assert r.cuota_integra == D("46000.00")
    # Short period (73 days = 1/5 of 365): 1 M x 73 / 365 = 200.000; 70 % x 1.200.000 = 840.000 prevails.
    r = _is(
        resultado_contable=D(1200000),
        cifra_negocios_anterior=D(8000000),
        bases_negativas_pendientes=D(1800000),
        dias_periodo=73,
    )
    assert r.bases_negativas_compensadas == D("840000.00")
    assert _aviso(r, "art. 26.2")


def test_losses_limits_for_large_turnover_and_new_companies() -> None:
    kwargs = {"resultado_contable": D(4000000), "bases_negativas_pendientes": D(5000000)}
    # Turnover 20 M: 50 % x 4 M = 2 M. Turnover 60 M: 25 % x 4 M = 1 M (equal to the 1 M floor).
    r = _is(cifra_negocios_anterior=D(20000000), **kwargs)
    assert r.bases_negativas_compensadas == D("2000000.00")
    assert _aviso(r, "art. 30 bis")  # minimum taxation is not computed
    assert _is(cifra_negocios_anterior=D(60000000), **kwargs).bases_negativas_compensadas == D("1000000.00")
    # Turnover unknown: 70 % x 4 M = 2,8 M, with a warning about the lower limits.
    r = _is(**kwargs)
    assert r.bases_negativas_compensadas == D("2800000.00")
    assert _aviso(r, "disposición adicional 15.ª")
    # Art. 26.3: no percentage limit for a newly created entity -> the whole base.
    r = _is(cifra_negocios_anterior=D(3000000), nueva_creacion_sin_limite_bases_negativas=True, **kwargs)
    assert r.bases_negativas_compensadas == D("4000000.00")
    assert r.base_imponible == 0


def test_seventy_percent_is_measured_before_the_capitalisation_reserve() -> None:
    # Base previa 2.000.000; reserve 20 % x 1.000.000 = 200.000 (limit 400.000). After reserve 1.800.000.
    # Losses limit 70 % x 2.000.000 (base BEFORE the reserve) = 1.400.000. Base 400.000 x 23 % = 92.000.
    r = _is(
        resultado_contable=D(2000000),
        cifra_negocios_anterior=D(8000000),
        incremento_fondos_propios=D(1000000),
        bases_negativas_pendientes=D(3000000),
    )
    assert r.reduccion_reserva_capitalizacion == D("200000.00")
    assert r.bases_negativas_compensadas == D("1400000.00")
    assert r.cuota_integra == D("92000.00")


# --- Levelling reserve (art. 105) ------------------------------------------------


def test_levelling_reserve_is_only_applied_on_request_and_only_on_the_micro_scale() -> None:
    kwargs = {"resultado_contable": D(80000), "cifra_negocios_anterior": D(300000)}
    # Not asked: nothing changes (15.800, see test_micro_company_scale_2026) and the result says it exists.
    r = _is(**kwargs)
    assert (r.minoracion_reserva_nivelacion, r.base_imponible_tras_nivelacion) == (0, D("80000.00"))
    assert _aviso(r, "No se ha aplicado la reserva de nivelación porque no se ha pedido")
    # Maximum: 10 % x 80.000 = 8.000. Base 72.000: 50.000 x 19 % = 9.500; 22.000 x 21 % = 4.620; total 14.120.
    r = _is(reserva_nivelacion_maxima=True, **kwargs)
    assert r.base_imponible == D("80000.00")
    assert r.minoracion_reserva_nivelacion == D("8000.00")
    assert r.base_imponible_tras_nivelacion == D("72000.00")
    assert r.cuota_integra == D("14120.00")
    assert r.reserva_nivelacion_pendiente == D("8000.00")
    assert r.tipo_medio == D("19.61")  # 14.120 / 72.000
    assert _aviso(r, "no es un ahorro definitivo sino un diferimiento")
    assert _aviso(r, "«apliquen el tipo de gravamen previsto en el primer párrafo del apartado 1 del artículo 29»")
    assert _aviso(r, "Confírmalo antes de dotar la reserva")
    # A declared amount: 3.000 -> base 77.000: 9.500 + 27.000 x 21 % = 5.670; total 15.170.
    assert _is(reserva_nivelacion=D(3000), **kwargs).cuota_integra == D("15170.00")
    # More than the maximum: capped at 8.000, and said.
    r = _is(reserva_nivelacion=D(20000), **kwargs)
    assert r.minoracion_reserva_nivelacion == D("8000.00")
    assert _aviso(r, "supera el máximo")


@pytest.mark.parametrize(
    ("kwargs", "cuota", "motivo"),
    [
        # Reduced-size rate (23 % x 80.000): the law's wording and the official forms disagree -> not applied.
        ({"cifra_negocios_anterior": D(3000000)}, "18400.00", "La ley y los modelos no dicen lo mismo"),
        # 15 % of a newly created entity: another paragraph of art. 29.1.
        (
            {"cifra_negocios_anterior": D(300000), "nueva_creacion_tipo_reducido": True},
            "12000.00",
            "está en otro párrafo del art. 29.1",
        ),
        ({"cifra_negocios_anterior": D(300000), "entidad_patrimonial": True}, "20000.00", "entidad patrimonial"),
        ({}, "20000.00", "no se ha indicado la cifra de negocios"),
        ({"cifra_negocios_anterior": D(10000000)}, "20000.00", "no cumple las condiciones del art. 101"),
    ],
)
def test_levelling_reserve_is_refused_outside_the_micro_scale(kwargs: dict, cuota: str, motivo: str) -> None:
    r = _is(resultado_contable=D(80000), reserva_nivelacion_maxima=True, **kwargs)
    assert r.minoracion_reserva_nivelacion == 0
    assert r.cuota_integra == D(cuota)
    assert _aviso(r, "Has pedido la reserva de nivelación y NO se ha aplicado")
    assert _aviso(r, motivo)
    # Without asking, the same explanation is given as plain information.
    assert _aviso(_is(resultado_contable=D(80000), **kwargs), "No se aplica la reserva de nivelación del art. 105 LIS")


def test_levelling_reserve_comes_after_the_capitalisation_reserve_and_the_losses() -> None:
    # Base previa 100.000; capitalisation 20 % x 20.000 = 4.000; losses 10.000 -> base imponible 86.000.
    # Levelling: 10 % x 86.000 = 8.600 -> 77.400: 9.500 + 27.400 x 21 % = 5.754; total 15.254.
    r = _is(
        resultado_contable=D(100000),
        cifra_negocios_anterior=D(300000),
        incremento_fondos_propios=D(20000),
        bases_negativas_pendientes=D(10000),
        reserva_nivelacion_maxima=True,
    )
    assert r.base_imponible == D("86000.00")
    assert r.minoracion_reserva_nivelacion == D("8600.00")
    assert r.cuota_integra == D("15254.00")
    # Nothing to reduce when the base is not positive.
    r = _is(resultado_contable=D(-5000), cifra_negocios_anterior=D(300000), reserva_nivelacion_maxima=True)
    assert r.minoracion_reserva_nivelacion == 0
    assert _aviso(r, "solo puede minorarse una base imponible positiva")


def test_levelling_reserve_ceiling_is_prorated_in_a_short_period() -> None:
    # 73 days: ceiling 1 M x 73 / 365 = 200.000 (< 10 % x 3 M = 300.000). Base 2.800.000.
    # First bracket 50.000 x 73 / 365 = 10.000 x 19 % = 1.900; 2.790.000 x 21 % = 585.900; total 587.800.
    r = _is(
        resultado_contable=D(3000000),
        cifra_negocios_anterior=D(900000),
        dias_periodo=73,
        reserva_nivelacion_maxima=True,
    )
    assert r.minoracion_reserva_nivelacion == D("200000.00")
    assert r.cuota_integra == D("587800.00")
    assert _aviso(r, "art. 105.1 LIS); se ha prorrateado por días")


def test_levelling_reserve_is_added_back_to_a_negative_base_and_at_the_end_of_the_term() -> None:
    # Loss of 30.000 with 12.000 pending: 12.000 added, the loss carried forward is 18.000.
    r = _is(resultado_contable=D(-30000), cifra_negocios_anterior=D(300000), reserva_nivelacion_pendiente=D(12000))
    assert r.adicion_reserva_nivelacion == D("12000.00")
    assert (r.base_imponible, r.base_imponible_tras_nivelacion) == (D("-30000.00"), D("-18000.00"))
    assert (r.bases_negativas_pendientes, r.reserva_nivelacion_pendiente, r.cuota_integra) == (D("18000.00"), 0, 0)
    # With 50.000 pending only 30.000 are added ("hasta el importe de la misma"): base 0, 20.000 still pending.
    r = _is(resultado_contable=D(-30000), cifra_negocios_anterior=D(300000), reserva_nivelacion_pendiente=D(50000))
    assert (r.adicion_reserva_nivelacion, r.base_imponible_tras_nivelacion) == (D("30000.00"), 0)
    assert (r.bases_negativas_pendientes, r.reserva_nivelacion_pendiente) == (0, D("20000.00"))
    # Positive base, 4.000 of the 10.000 pending reach the end of the 5 years: 44.000 x 19 % = 8.360.
    kwargs = {
        "resultado_contable": D(40000),
        "cifra_negocios_anterior": D(300000),
        "reserva_nivelacion_pendiente": D(10000),
        "reserva_nivelacion_vence": D(4000),
    }
    r = _is(**kwargs)
    assert (r.adicion_reserva_nivelacion, r.cuota_integra, r.reserva_nivelacion_pendiente) == (
        D("4000.00"),
        D("8360.00"),
        D("6000.00"),
    )
    # The same year a new reduction of 10 % of the base imponible (40.000) = 4.000: taxed on 40.000 -> 7.600.
    r = _is(reserva_nivelacion_maxima=True, **kwargs)
    assert (r.minoracion_reserva_nivelacion, r.cuota_integra, r.reserva_nivelacion_pendiente) == (
        D("4000.00"),
        D("7600.00"),
        D("10000.00"),
    )
    # The additions are an obligation whatever the rate now is: reduced-size 23 % x 44.000 = 10.120.
    assert _is(**{**kwargs, "cifra_negocios_anterior": D(3000000)}).cuota_integra == D("10120.00")
    with pytest.raises(ValueError, match="vence"):
        SociedadesInput(ejercicio=2026, reserva_nivelacion_vence=D(1))
    with pytest.raises(ValueError, match="no las dos cosas"):
        SociedadesInput(ejercicio=2026, reserva_nivelacion=D(1), reserva_nivelacion_maxima=True)


# --- Quota to pay --------------------------------------------------------------


def test_credits_withholdings_and_instalments() -> None:
    # Micro, base 80.000 -> 15.800. Credits 1.800 -> líquida 14.000. 14.000 - 500 - 9.000 = 4.500.
    r = _is(
        resultado_contable=D(80000),
        cifra_negocios_anterior=D(300000),
        bonificaciones_deducciones=D(1800),
        retenciones_ingresos_cuenta=D(500),
        pagos_fraccionados=D(9000),
    )
    assert r.cuota_liquida == D("14000.00")
    assert r.cuota_diferencial == D("4500.00")
    assert [(line.concepto, line.importe) for line in r.lineas][-5:] == [
        ("Bonificaciones y deducciones", D("-1800.00")),
        ("Cuota líquida", D("14000.00")),
        ("Retenciones e ingresos a cuenta", D("-500.00")),
        ("Pagos fraccionados", D("-9000.00")),
        ("Cuota diferencial (a ingresar si es positiva, a devolver si es negativa)", D("4500.00")),
    ]


def test_net_quota_cannot_be_negative_but_payments_are_refunded() -> None:
    # 40.000 x 19 % = 7.600; credits 9.000 -> líquida 0 (art. 30.2); 0 - 1.000 = -1.000 refund.
    r = _is(
        resultado_contable=D(40000),
        cifra_negocios_anterior=D(300000),
        bonificaciones_deducciones=D(9000),
        pagos_fraccionados=D(1000),
    )
    assert r.cuota_liquida == 0
    assert r.cuota_diferencial == D("-1000.00")
    assert _aviso(r, "1400.00 euros no se han aplicado")


def test_inputs_are_validated() -> None:
    with pytest.raises(ValueError):
        SociedadesInput(ejercicio=2026, ajustes_positivos=D(-1))
    with pytest.raises(ValueError):
        SociedadesInput(ejercicio=2026, desconocido=1)  # type: ignore[call-arg]


# --- Instalments (art. 40, modelo 202) ------------------------------------------


def test_instalment_on_the_previous_quota() -> None:
    # (12.000 - 1.000 - 500) = 10.500 x 18 % = 1.890.
    r = _pf(
        cuota_integra_ultimo_periodo=D(12000),
        deducciones_bonificaciones_ultimo_periodo=D(1000),
        retenciones_ultimo_periodo=D(500),
        cifra_negocios_anterior=D(400000),
    )
    assert (r.base_pago, r.porcentaje, r.importe) == (D("10500.00"), D("0.18"), D("1890.00"))
    assert _aviso(r, "modelo 202")
    # Negative base: nothing to pay.
    assert _pf(cuota_integra_ultimo_periodo=D(100), retenciones_ultimo_periodo=D(500)).importe == 0


def test_instalment_on_the_previous_quota_cases_without_a_figure() -> None:
    r = _pf(cuota_integra_ultimo_periodo=D(12000), ultimo_periodo_inferior_al_ano=True)
    assert r.importe is None
    assert _aviso(r, "hasta completar 12 meses")
    # Turnover above 6 M in the previous 12 months: the base method is compulsory.
    r = _pf(cuota_integra_ultimo_periodo=D(12000), cifra_negocios_anterior=D(6000001))
    assert r.importe is None
    assert _aviso(r, "es obligatoria la modalidad del art. 40.3")
    assert _pf(cuota_integra_ultimo_periodo=D(12000), cifra_negocios_anterior=D(6000000)).importe == D("2160.00")


def test_instalment_on_the_current_base() -> None:
    # Reduced-size 2026: 23 % x 5/7 = 16,43 -> 16 %. 100.000 x 16 % = 16.000 - 200 - 1.000 - 5.000 = 9.800.
    r = _pf(
        modalidad="base",
        cifra_negocios_anterior=D(3000000),
        base_imponible_acumulada=D(100000),
        bonificaciones=D(200),
        retenciones_acumuladas=D(1000),
        pagos_fraccionados_anteriores=D(5000),
    )
    assert (r.porcentaje, r.importe) == (D("0.16"), D("9800.00"))
    assert _aviso(r, "entero inferior")
    # General rate: 25 % x 5/7 = 17,86 -> 17 %. Newly created: 15 % x 5/7 = 10,71 -> 10 %.
    assert _pf(modalidad="base", base_imponible_acumulada=D(100000)).importe == D("17000.00")
    r = _pf(modalidad="base", base_imponible_acumulada=D(100000), nueva_creacion_tipo_reducido=True)
    assert r.importe == D("10000.00")
    # 2027 reduced-size: 22 % x 5/7 = 15,71 -> 15 %.
    r = _pf(ejercicio=2027, modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(3000000))
    assert r.importe == D("15000.00")
    # Negative base or more withheld than due: zero.
    assert _pf(modalidad="base", base_imponible_acumulada=D(-5000)).importe == 0
    assert _pf(modalidad="base", base_imponible_acumulada=D(1000), retenciones_acumuladas=D(900)).importe == 0


def test_instalment_on_the_base_for_the_micro_scale_takes_five_sevenths_of_each_rate() -> None:
    # 2026 scale 19 % / 21 %: 19 x 5/7 = 13,57 -> 13 %; 21 x 5/7 = 15 -> 15 %.
    # 100.000: 50.000 x 13 % = 6.500; 50.000 x 15 % = 7.500; total 14.000 - 1.000 withheld - 2.000 paid = 11.000.
    r = _pf(
        modalidad="base",
        base_imponible_acumulada=D(100000),
        cifra_negocios_anterior=D(500000),
        retenciones_acumuladas=D(1000),
        pagos_fraccionados_anteriores=D(2000),
    )
    assert (r.porcentaje, r.porcentaje_resto, r.base_pago, r.importe) == (
        D("0.13"),
        D("0.15"),
        D("100000.00"),
        D("11000.00"),
    )
    assert _aviso(r, "«B.2) Casos específicos (entidades con más de un porcentaje)»")
    assert _aviso(r, "NINGUNA norma leída dice es cómo se reparte la base acumulada")
    assert _aviso(r, "el pago sería 1000.00 euros mayor")  # 50.000 x (15 % - 13 %)
    assert "BOE-A-2025-5407" in r.fuente
    # Base inside the first bracket: 30.000 x 13 % = 3.900.
    r = _pf(modalidad="base", base_imponible_acumulada=D(30000), cifra_negocios_anterior=D(0))
    assert (r.importe, r.porcentaje_resto) == (D("3900.00"), D("0.15"))
    assert _aviso(r, "el pago sería 600.00 euros mayor")  # 30.000 x 2 %
    # 2025 scale 21 % / 22 %: 15 % and 15,71 -> 15 %: 100.000 x 15 % = 15.000, whatever the split.
    r = _pf(ejercicio=2025, modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(500000))
    assert (r.porcentaje, r.porcentaje_resto, r.importe) == (D("0.15"), D("0.15"), D("15000.00"))
    assert _aviso(r, "los dos porcentajes coinciden")
    # 2027 scale 17 % / 20 %: 12,14 -> 12 % and 14,29 -> 14 %: 6.000 + 7.000 = 13.000.
    r = _pf(ejercicio=2027, modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(500000))
    assert (r.porcentaje, r.porcentaje_resto, r.importe) == (D("0.12"), D("0.14"), D("13000.00"))
    # Tax period of 73 days: first bracket 50.000 x 73 / 365 = 10.000. 10.000 x 13 % + 90.000 x 15 % = 14.800.
    r = _pf(modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(500000), dias_periodo=73)
    assert r.importe == D("14800.00")
    # A single-rate result has no second percentage.
    assert _pf(modalidad="base", base_imponible_acumulada=D(100000)).porcentaje_resto is None


def test_instalment_on_the_base_is_not_guessed_for_large_turnover() -> None:
    r = _pf(modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(10000000))
    assert r.importe is None
    assert _aviso(r, "disposición adicional 14.ª")


# --- Self-employed vs company -----------------------------------------------------


def test_comparison_adds_up_and_does_not_pick_a_winner() -> None:
    # Profit 80.000, salary 30.000, turnover 120.000 (micro).
    # Company: 50.000 x 19 % = 9.500; after tax 40.500, all distributed.
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026,
            beneficio_antes_retribucion=D(80000),
            salario=D(30000),
            cifra_negocios_anterior=D(120000),
        )
    )
    autonomo = calcular_irpf(IRPFInput(ejercicio=2026, rendimiento_actividad_neto=D(80000)))
    socio = calcular_irpf(
        IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=D(30000), rendimiento_capital_mobiliario=D(40500))
    )
    assert c.autonomo_irpf == autonomo.resultado
    assert c.sociedad_impuesto_sociedades == D("9500.00")
    assert c.beneficio_despues_impuesto == D("40500.00")
    assert c.dividendo == D("40500.00")
    assert c.beneficio_retenido == 0
    assert c.sociedad_irpf_socio == socio.resultado
    assert c.sociedad_total == D("9500.00") + socio.resultado
    assert c.diferencia == c.sociedad_total - c.autonomo_irpf
    # Loud about what is ignored...
    assert c.avisos[0].startswith("ESTA COMPARACIÓN SOLO SUMA IMPUESTOS")
    for fragmento in (
        "Seguridad Social",
        "art. 27.1 LIRPF",
        "IVA",
        "contabilidad",
        "NO reparte no tributa en el IRPF del socio hasta que se distribuya",
    ):
        assert _aviso(c, fragmento), fragmento
    # ... keeps the caveats of the three underlying computations...
    assert _aviso(c, "Sociedad (IS): Escala de microempresa")
    assert _aviso(c, "Autónomo (IRPF): ")
    # ... and gives no verdict.
    texto = " ".join(c.avisos + [line.concepto for line in c.lineas]).lower()
    for palabra in ("te conviene", "mejor opción", "es preferible", "ahorras", "ganador"):
        assert palabra not in texto


def test_comparison_with_retained_profit() -> None:
    # Profit 120.000, salary 40.000 -> company base 80.000 -> 15.800 (micro 2026); after tax 64.200.
    # Dividend 10.000 -> 54.200 stays in the company and is not taxed on the owner yet.
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026,
            beneficio_antes_retribucion=D(120000),
            salario=D(40000),
            dividendo=D(10000),
            cifra_negocios_anterior=D(200000),
            comunidad_autonoma="madrid",
        )
    )
    socio = calcular_irpf(
        IRPFInput(
            ejercicio=2026,
            comunidad_autonoma="madrid",
            rendimiento_trabajo_bruto=D(40000),
            rendimiento_capital_mobiliario=D(10000),
        )
    )
    assert c.sociedad_impuesto_sociedades == D("15800.00")
    assert c.beneficio_despues_impuesto == D("64200.00")
    assert c.beneficio_retenido == D("54200.00")
    assert c.sociedad_irpf_socio == socio.resultado
    assert c.sociedad_total == D("15800.00") + socio.resultado
    assert c.detalle_autonomo.comunidad_aplicada == "Comunidad de Madrid"
    assert ("(b) Beneficio que queda en la sociedad sin repartir", D("54200.00")) in [
        (line.concepto, line.importe) for line in c.lineas
    ]


def test_comparison_without_cifra_uses_the_general_rate_and_rejects_impossible_figures() -> None:
    # No turnover given: 25 % of 50.000 = 12.500.
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(ejercicio=2026, beneficio_antes_retribucion=D(50000), salario=D(0))
    )
    assert c.sociedad_impuesto_sociedades == D("12500.00")
    assert c.dividendo == D("37500.00")
    with pytest.raises(ValueError, match="salario"):
        comparar_autonomo_vs_sociedad(
            ComparacionInput(ejercicio=2026, beneficio_antes_retribucion=D(50000), salario=D(60000))
        )
    with pytest.raises(ValueError, match="dividendo"):
        comparar_autonomo_vs_sociedad(
            ComparacionInput(ejercicio=2026, beneficio_antes_retribucion=D(50000), dividendo=D(40000))
        )


def test_comparison_legal_references_and_dividend_withholding() -> None:
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026,
            beneficio_antes_retribucion=D(80000),
            salario=D(30000),
            cifra_negocios_anterior=D(120000),
        )
    )
    referencias = {line.concepto: line.referencia for line in c.lineas}
    assert referencias["(b) Socio: salario (rendimiento íntegro del trabajo)"] == "art. 17.1 LIRPF"
    assert (
        referencias["(b) Socio: dividendo (rendimiento del capital mobiliario, renta del ahorro)"]
        == "arts. 25.1.a) y 46.a) LIRPF"
    )
    # 19 % x 40.500 = 7.695: a payment on account, already inside the owner's IRPF.
    assert c.retencion_dividendo == D("7695.00")
    assert _aviso(c, "no tiene hoy deducción por doble imposición de dividendos ni exención")
    assert _aviso(c, "retiene el 19 % (art. 101.4 LIRPF; aquí 7695.00 euros)")
    # The art. 27.1 rule is quoted, not modelled.
    assert _aviso(c, "tendrán esta consideración cuando el contribuyente esté incluido, a tal efecto, en el régimen")
    # No contributions given: none in the result, and the warning stays.
    assert (c.autonomo_cotizaciones, c.sociedad_cotizaciones, c.diferencia_con_cotizaciones) == (None, None, None)
    assert _aviso(c, "NO se incluyen las cotizaciones a la Seguridad Social")
    assert "BOE-A-2006-20764" in c.fuente


def test_comparison_with_the_contributions_the_user_supplies() -> None:
    # Profit 80.000. (a) RETA 5.000 -> net activity income 75.000.
    # (b) salary 30.000, employer 9.500 -> company base 40.500 x 19 % = 7.695; after tax 32.805, all distributed.
    #     Owner: work income 30.000 with 1.900 of contributions deducted, dividend 32.805.
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026,
            beneficio_antes_retribucion=D(80000),
            salario=D(30000),
            cifra_negocios_anterior=D(120000),
            cuota_autonomos=D(5000),
            cotizaciones_socio=D(1900),
            cotizaciones_empresa=D(9500),
        )
    )
    autonomo = calcular_irpf(IRPFInput(ejercicio=2026, rendimiento_actividad_neto=D(75000)))
    socio = calcular_irpf(
        IRPFInput(
            ejercicio=2026,
            rendimiento_trabajo_bruto=D(30000),
            cotizaciones_seguridad_social=D(1900),
            rendimiento_capital_mobiliario=D(32805),
        )
    )
    assert c.autonomo_irpf == autonomo.resultado
    assert c.sociedad_impuesto_sociedades == D("7695.00")
    assert c.beneficio_despues_impuesto == D("32805.00")
    assert c.sociedad_irpf_socio == socio.resultado
    assert c.diferencia == c.sociedad_total - c.autonomo_irpf
    assert (c.autonomo_cotizaciones, c.sociedad_cotizaciones) == (D("5000.00"), D("11400.00"))
    assert c.diferencia_con_cotizaciones == c.sociedad_total + D(11400) - c.autonomo_irpf - D(5000)
    assert _aviso(c, "Las cotizaciones a la Seguridad Social son las cifras que indicas")
    assert not _aviso(c, "NO se incluyen las cotizaciones")
    assert not _aviso(c, "NO has indicado")
    # Lower taxes than without contributions on both sides (they are deductible).
    sin = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026, beneficio_antes_retribucion=D(80000), salario=D(30000), cifra_negocios_anterior=D(120000)
        )
    )
    assert c.autonomo_irpf < sin.autonomo_irpf and c.sociedad_total < sin.sociedad_total

    # Only one side given: it enters that side, there is no joint difference, and the result says what is missing.
    c = comparar_autonomo_vs_sociedad(
        ComparacionInput(
            ejercicio=2026,
            beneficio_antes_retribucion=D(80000),
            salario=D(30000),
            cifra_negocios_anterior=D(120000),
            cuota_autonomos=D(5000),
        )
    )
    assert c.autonomo_irpf == autonomo.resultado
    assert (c.autonomo_cotizaciones, c.sociedad_cotizaciones, c.diferencia_con_cotizaciones) == (
        D("5000.00"),
        None,
        None,
    )
    assert _aviso(c, "NO has indicado las cotizaciones que paga el socio por su salario (opción sociedad); ni las")
    with pytest.raises(ValueError, match="salario"):
        comparar_autonomo_vs_sociedad(
            ComparacionInput(
                ejercicio=2026, beneficio_antes_retribucion=D(50000), salario=D(45000), cotizaciones_empresa=D(6000)
            )
        )


def test_every_literal_quote_shown_to_the_user_is_in_the_downloaded_texts() -> None:
    # Whatever the calculators print between «» must be in the extracts, word for word.
    textos = " ".join(" ".join(texto.split()) for texto in textos_de_directorio(EXTRACTOS).values()).lower()
    resultados = [
        _is(resultado_contable=D(80000), cifra_negocios_anterior=D(300000), reserva_nivelacion_maxima=True),
        _is(resultado_contable=D(80000), cifra_negocios_anterior=D(3000000), reserva_nivelacion_maxima=True),
        _is(resultado_contable=D(80000), cifra_negocios_anterior=D(300000), nueva_creacion_tipo_reducido=True),
        _is(resultado_contable=D(80000), cifra_negocios_anterior=D(300000), entidad_patrimonial=True),
        _is(
            resultado_contable=D(1200000),
            cifra_negocios_anterior=D(30000000),
            bases_negativas_pendientes=D(1800000),
            incremento_fondos_propios=D(1000),
            nueva_creacion_sin_limite_bases_negativas=True,
            dias_periodo=73,
        ),
        _is(resultado_contable=D(1200000), bases_negativas_pendientes=D(1800000), dias_periodo=73),
        _is(
            resultado_contable=D(3000000), cifra_negocios_anterior=D(0), dias_periodo=73, reserva_nivelacion_maxima=True
        ),
        _pf(modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(500000)),
        _pf(modalidad="base", base_imponible_acumulada=D(100000), cifra_negocios_anterior=D(5000000)),
        comparar_autonomo_vs_sociedad(
            ComparacionInput(ejercicio=2026, beneficio_antes_retribucion=D(80000), salario=D(30000))
        ),
    ]
    citas = {cita for r in resultados for aviso in r.avisos for cita in re.findall(r"«([^»]+)»", aviso)}
    assert len(citas) >= 12
    for cita in sorted(citas):
        assert cita.removesuffix("...").lower() in textos, cita
