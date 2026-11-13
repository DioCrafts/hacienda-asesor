"""Self-employed calculators (estimación directa simplificada, modelo 130,
modelo 303). Expected figures are worked by hand in the comments from the
parameters in ``hacienda_gpt/tax/data/autonomos_2026.json``, which are in turn
re-read here against literal extracts of the consolidated laws."""

from decimal import Decimal
import os
from pathlib import Path

import pytest

from hacienda_gpt.tax.actividad import ActividadInput, calcular_rendimiento_actividad, tipo_retencion_profesional
from hacienda_gpt.tax.autonomos_params import (
    AutonomosParams,
    cifra_a_decimal,
    comprobar_evidencias,
    ejercicios_autonomos_disponibles,
    load_autonomos_params,
    texto_de_html,
    textos_de_directorio,
)
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.iva import Modelo303Input, calcular_modelo_303
from hacienda_gpt.tax.pagos_fraccionados import Modelo130Input, calcular_modelo_130
from hacienda_gpt.tax.params import EjercicioNoDisponibleError

D = Decimal
EXTRACTOS = Path(__file__).resolve().parents[1] / "fixtures" / "boe_autonomos"


def _actividad(**kwargs):
    return calcular_rendimiento_actividad(ActividadInput(ejercicio=2026, **kwargs))


def _m130(**kwargs):
    return calcular_modelo_130(Modelo130Input(ejercicio=2026, **kwargs))


def _m303(**kwargs):
    return calcular_modelo_303(Modelo303Input(ejercicio=2026, **kwargs))


def _linea(result, prefix: str) -> D:
    return next(line.importe for line in result.lineas if line.concepto.startswith(prefix))


def _avisa(result, fragment: str) -> bool:
    return any(fragment in aviso for aviso in result.avisos)


# --- Parameters and their evidence -------------------------------------------


def test_params_load_with_the_figures_read_from_the_law() -> None:
    assert ejercicios_autonomos_disponibles() == [2025, 2026]
    p = load_autonomos_params(2026)
    assert (p.gastos_dificil_justificacion.porcentaje, p.gastos_dificil_justificacion.limite_anual) == (D("0.05"), 2000)
    assert p.pago_fraccionado.porcentaje == D("0.20")
    assert p.pago_fraccionado.exencion_porcentaje_retenido == D("0.70")
    assert (p.retencion_profesionales.general, p.retencion_profesionales.inicio_actividad) == (D("0.15"), D("0.07"))
    assert (p.iva.tipo_general, p.iva.tipo_reducido, p.iva.tipo_superreducido) == (D("0.21"), D("0.10"), D("0.04"))
    assert all(grupo.fuente.verificado for grupo in p.grupos().values())
    assert tipo_retencion_profesional(p) == D("0.15")
    assert tipo_retencion_profesional(p, inicio_actividad=True) == D("0.07")
    with pytest.raises(EjercicioNoDisponibleError):
        load_autonomos_params(1999)


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    textos = textos_de_directorio(EXTRACTOS)
    assert set(textos) == set(load_autonomos_params(2026).fuentes_principales)
    assert comprobar_evidencias(load_autonomos_params(2026), textos) == []


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point HACIENDA_BOE_PAGINAS at a directory with BOE-A-*.html straight from boe.es.
    directorio = os.environ.get("HACIENDA_BOE_PAGINAS")
    if not directorio:
        pytest.skip("HACIENDA_BOE_PAGINAS no está definido")
    assert comprobar_evidencias(load_autonomos_params(2026), textos_de_directorio(Path(directorio))) == []


def test_self_check_catches_a_wrong_figure_a_wrong_quote_and_a_figure_without_evidence() -> None:
    p = load_autonomos_params(2026)
    textos = textos_de_directorio(EXTRACTOS)
    raw = p.model_dump(mode="json")

    wrong = {**raw, "gastos_dificil_justificacion": {**raw["gastos_dificil_justificacion"], "porcentaje": "0.07"}}
    problemas = comprobar_evidencias(AutonomosParams.model_validate(wrong), textos)
    assert problemas == ["gastos_dificil_justificacion.porcentaje: el dato es 0.07 y la cita dice «5 por ciento»"]

    evidencia = [{**raw["iva"]["evidencia"][0], "cita": "El Impuesto se exigirá al tipo del 21 por ciento siempre."}]
    invented = {**raw, "iva": {**raw["iva"], "evidencia": evidencia + raw["iva"]["evidencia"][1:]}}
    problemas = comprobar_evidencias(AutonomosParams.model_validate(invented), textos)
    assert problemas == ["iva: la cita de art. 90.uno LIVA no aparece literal en BOE-A-1992-28740"]

    orphan = {**raw, "iva": {**raw["iva"], "evidencia": raw["iva"]["evidencia"][1:]}}
    assert comprobar_evidencias(AutonomosParams.model_validate(orphan), textos) == [
        "iva.tipo_general: cifra sin evidencia"
    ]

    # "100" must be found as a figure of its own, not inside "10.000".
    tramos = raw["pago_fraccionado"]["evidencia"]
    recortada = [{**ev, "cita": ev["cita"].replace("9.000 100 Entre", "9.000 Entre")} for ev in tramos]
    sin_cien = {**raw, "pago_fraccionado": {**raw["pago_fraccionado"], "evidencia": recortada}}
    assert (
        "pago_fraccionado.minoracion_tramos.0.importe: «100» no aparece en la cita de art. 110.3.c) RIRPF"
        in comprobar_evidencias(AutonomosParams.model_validate(sin_cien))
    )


def test_reading_figures_as_the_law_spells_them() -> None:
    assert cifra_a_decimal("5 por ciento") == D("0.05")
    assert cifra_a_decimal("14.047,5 euros") == D("14047.5")
    assert cifra_a_decimal("0,405") == D("0.405")
    assert cifra_a_decimal("cuatro años") == 4
    assert texto_de_html("<p>al&nbsp;tipo del <strong>21</strong>\n por ciento</p><p>Dos.</p>") == (
        "al tipo del 21 por ciento\nDos."
    )


# --- Net income, estimación directa simplificada -----------------------------


def test_net_income_with_expenses_by_category_and_flat_allowance() -> None:
    r = _actividad(
        ingresos_integros=D("40000"),
        seguridad_social_autonomo=D("3600"),
        suministros=D("600"),
        alquileres=D("4800"),
        servicios_profesionales=D("1000"),
        otros_gastos=D("2000"),
        amortizaciones=D("1000"),
    )
    # Expenses: 3,600 + 600 + 4,800 + 1,000 + 2,000 + 1,000 = 13,000
    # Difference: 40,000 - 13,000 = 27,000
    # Flat allowance: 5 % x 27,000 = 1,350 (under the 2,000 cap)
    # Net income: 27,000 - 1,350 = 25,650; no art. 32.2 reduction
    assert r.gastos_deducibles == D("13000.00")
    assert r.rendimiento_neto_previo == D("27000.00")
    assert r.gastos_dificil_justificacion == D("1350.00")
    assert r.rendimiento_neto == D("25650.00")
    assert r.reduccion_art_32_2 == 0 and r.reduccion_aplicada is None
    assert r.rendimiento_actividad_neto == D("25650.00")
    assert _linea(r, "Provisiones deducibles") == D("-1350.00")
    assert next(line for line in r.lineas if line.concepto.startswith("Provisiones")).referencia.startswith(
        "art. 30.2.ª RIRPF"
    )
    assert _avisa(r, "no valora si son deducibles")
    assert _avisa(r, "Ceuta")
    assert _avisa(r, "módulos")
    assert r.parametros_no_verificados == []
    assert "BOE-A-2007-6820" in r.fuente


def test_flat_allowance_is_capped_and_is_zero_on_a_loss() -> None:
    # 80,000 - 20,000 = 60,000; 5 % = 3,000 -> capped at 2,000; net 58,000
    r = _actividad(ingresos_integros=D("80000"), otros_gastos=D("20000"))
    assert r.gastos_dificil_justificacion == D("2000")
    assert r.rendimiento_neto == D("58000.00")
    # 5,000 - 8,000 = -3,000: nothing to take a percentage of, and no reduction on a loss
    r = _actividad(ingresos_integros=D("5000"), otros_gastos=D("8000"), otras_rentas_no_exentas=D("0"))
    assert r.gastos_dificil_justificacion == 0
    assert r.rendimiento_actividad_neto == D("-3000.00")
    assert r.reduccion_art_32_2 == 0


def test_dependent_self_employed_reduction_replaces_the_flat_allowance() -> None:
    kwargs = dict(
        ingresos_integros=D("20000"),
        seguridad_social_autonomo=D("4000"),
        cumple_requisitos_reduccion_dependiente=True,
        otras_rentas_no_exentas=D("0"),
    )
    r = _actividad(**kwargs)
    # Expenses 4,000 = 20 % of income (limit 30 %). Net: 20,000 - 4,000 = 16,000, no flat allowance.
    # General: 2,000
    # Additional (16,000 is between 14,047.5 and 19,747.5):
    #   6,498 - 1.14 x (16,000 - 14,047.5) = 6,498 - 1.14 x 1,952.5 = 6,498 - 2,225.85 = 4,272.15
    # Total 6,272.15 -> 16,000 - 6,272.15 = 9,727.85
    assert r.gastos_dificil_justificacion == 0
    assert r.rendimiento_neto == D("16000.00")
    assert _linea(r, "Reducción adicional por rendimientos netos bajos") == D("-4272.15")
    assert r.reduccion_art_32_2 == D("6272.15")
    assert r.reduccion_aplicada == "art. 32.2.1.º LIRPF"
    assert r.rendimiento_actividad_neto == D("9727.85")

    # With a disability: + 3,500 -> 9,772.15 -> 16,000 - 9,772.15 = 6,227.85
    assert _actividad(**kwargs, discapacidad=True).rendimiento_actividad_neto == D("6227.85")
    # 65 % or more / help from third parties: + 7,750 -> 14,022.15 -> 1,977.85
    assert _actividad(**kwargs, discapacidad_ampliada=True).rendimiento_actividad_neto == D("1977.85")

    # Other income above 6,500: only the general 2,000 -> 14,000
    r = _actividad(**{**kwargs, "otras_rentas_no_exentas": D("6500.01")})
    assert r.rendimiento_actividad_neto == D("14000.00")
    # Other income unknown: only the general 2,000, and it says why
    r = _actividad(**{**kwargs, "otras_rentas_no_exentas": None})
    assert r.rendimiento_actividad_neto == D("14000.00")
    assert _avisa(r, "no se aplica la reducción adicional")


def test_dependent_reduction_thresholds_and_floor_at_zero() -> None:
    def reduccion(ingresos: str) -> D:
        return _actividad(
            ingresos_integros=D(ingresos), cumple_requisitos_reduccion_dependiente=True, otras_rentas_no_exentas=D("0")
        ).reduccion_art_32_2

    # Net <= 14,047.5: 2,000 + 6,498 = 8,498
    assert reduccion("14047.50") == D("8498.00")
    # Net 19,747.49 (< 19,747.5): 6,498 - 1.14 x 5,699.99 = 6,498 - 6,497.9886 = 0.0114 -> total 2,000.01
    assert reduccion("19747.49") == D("2000.01")
    # Net 19,747.5 is not "inferior a 19.747,5": only the general 2,000
    assert reduccion("19747.50") == D("2000.00")
    # Net 3,000: 8,498 would leave a negative balance -> limited to 3,000 (art. 32.2.4.º)
    r = _actividad(
        ingresos_integros=D("3000"), cumple_requisitos_reduccion_dependiente=True, otras_rentas_no_exentas=D("0")
    )
    assert r.reduccion_art_32_2 == D("3000.00")
    assert r.rendimiento_actividad_neto == 0


def test_dependent_reduction_refused_when_expenses_exceed_thirty_percent() -> None:
    r = _actividad(
        ingresos_integros=D("20000"),
        otros_gastos=D("7000"),
        cumple_requisitos_reduccion_dependiente=True,
        otras_rentas_no_exentas=D("0"),
    )
    # 7,000 > 30 % x 20,000 = 6,000 -> requirement c) fails; ordinary rules:
    # 13,000 - 5 % x 13,000 (650) = 12,350; income 12,350 is not under 12,000 -> no art. 32.2.3.º either
    assert _avisa(r, "superan el 30 % de los ingresos íntegros")
    assert r.gastos_dificil_justificacion == D("650.00")
    assert r.reduccion_art_32_2 == 0
    assert r.rendimiento_actividad_neto == D("12350.00")


def test_low_income_reduction() -> None:
    # 6,000 - 1,000 = 5,000; flat allowance 250; net 4,750; no other income
    # Income 4,750 <= 8,000 -> 1,620 -> 3,130
    r = _actividad(ingresos_integros=D("6000"), otros_gastos=D("1000"), otras_rentas_no_exentas=D("0"))
    assert r.reduccion_art_32_2 == D("1620.00")
    assert r.reduccion_aplicada == "art. 32.2.3.º LIRPF"
    assert r.rendimiento_actividad_neto == D("3130.00")

    # 12,000 - 2,000 = 10,000; flat allowance 500; net 9,500; other income 500 -> 10,000
    # 1,620 - 0.405 x (10,000 - 8,000) = 1,620 - 810 = 810 -> 9,500 - 810 = 8,690
    kwargs = dict(ingresos_integros=D("12000"), otros_gastos=D("2000"), otras_rentas_no_exentas=D("500"))
    r = _actividad(**kwargs)
    assert r.reduccion_art_32_2 == D("810.00")
    assert r.rendimiento_actividad_neto == D("8690.00")
    assert _avisa(r, "reducción del art. 20 LIRPF")
    # Joint cap with the work reduction: 3,700 - 3,200 = 500 -> 9,500 - 500 = 9,000
    assert _actividad(**kwargs, reduccion_rendimientos_trabajo=D("3200")).rendimiento_actividad_neto == D("9000.00")
    # Other income 2,500 -> 12,000, which is not under 12,000 -> nothing
    assert _actividad(**{**kwargs, "otras_rentas_no_exentas": D("2500")}).reduccion_art_32_2 == 0
    # Other income not given: not applied, and it says it might be due
    r = _actividad(**{**kwargs, "otras_rentas_no_exentas": None})
    assert r.reduccion_art_32_2 == 0
    assert _avisa(r, "puede corresponder la reducción del art. 32.2.3.º")


def test_turnover_limit_warning_and_result_feeds_the_irpf_calculator() -> None:
    r = _actividad(ingresos_integros=D("1000"))
    assert _avisa(r, "no se ha comprobado") and r.modalidad_simplificada_aplicable is None
    r = _actividad(ingresos_integros=D("700000"), cifra_negocios_ano_anterior=D("600000.01"))
    assert _avisa(r, "no corresponde la modalidad simplificada") and r.modalidad_simplificada_aplicable is False
    r = _actividad(ingresos_integros=D("1"), cifra_negocios_ano_anterior=D("600000"))  # "no supere": inclusive
    assert not _avisa(r, "no corresponde") and r.modalidad_simplificada_aplicable is True

    r = _actividad(ingresos_integros=D("40000"), otros_gastos=D("13000"))
    irpf = calcular_irpf(IRPFInput(ejercicio=2026, rendimiento_actividad_neto=r.rendimiento_actividad_neto))
    assert _linea(irpf, "Rendimiento neto de actividades económicas") == D("25650.00")
    assert irpf.base_imponible_general == D("25650.00")

    with pytest.raises(ValueError):
        ActividadInput(ejercicio=2026, ingresos_integros=D("-1"))
    with pytest.raises(ValueError):
        ActividadInput(ejercicio=2026, gastos=D("1"))  # unknown field


# --- Modelo 130 ---------------------------------------------------------------


def test_modelo_130_two_consecutive_quarters() -> None:
    q1 = _m130(
        trimestre=1, ingresos_acumulados=D("10000"), gastos_acumulados=D("3000"), retenciones_acumuladas=D("600")
    )
    # 10,000 - 3,000 = 7,000; flat allowance 5 % = 350; net 6,650
    # 20 % x 6,650 = 1,330; - withholdings 600 = 730
    assert q1.rendimiento_neto_acumulado == D("6650.00")
    assert q1.pago_sobre_rendimiento == D("1330.00")
    assert q1.resultado == q1.a_ingresar == q1.pago_previo_a_minoracion == D("730.00")
    assert q1.obligado_a_pago_fraccionado is None
    assert _avisa(q1, "No se ha podido determinar si estás obligado")
    assert _avisa(q1, "No se ha aplicado la minoración del art. 110.3.c) RIRPF (de 100 a 25 €")

    q2 = _m130(
        trimestre=2,
        ingresos_acumulados=D("22000"),
        gastos_acumulados=D("6000"),
        retenciones_acumuladas=D("1500"),
        pagos_trimestres_anteriores=q1.pago_previo_a_minoracion,
    )
    # 22,000 - 6,000 = 16,000; flat allowance 800; net 15,200
    # 20 % x 15,200 = 3,040; - Q1 730 - withholdings 1,500 = 810
    assert q2.resultado == D("810.00")
    assert _linea(q2, "Pagos fraccionados de los trimestres anteriores") == D("-730.00")


def test_modelo_130_is_never_negative_and_caps_the_flat_allowance() -> None:
    # 20 % x 6,650 = 1,330; withholdings 1,500 -> -170 -> zero, "declaración negativa"
    r = _m130(
        trimestre=1, ingresos_acumulados=D("10000"), gastos_acumulados=D("3000"), retenciones_acumuladas=D("1500")
    )
    assert r.resultado == r.a_ingresar == 0
    assert _avisa(r, "declaración negativa")
    # Cumulative loss: 2,000 - 5,000 < 0 -> 20 % of nothing
    r = _m130(trimestre=3, ingresos_acumulados=D("2000"), gastos_acumulados=D("5000"))
    assert r.rendimiento_neto_acumulado == D("-3000.00")
    assert r.pago_sobre_rendimiento == r.resultado == 0
    # 100,000 - 20,000 = 80,000; 5 % = 4,000 -> 2,000; net 78,000; 20 % = 15,600
    r = _m130(trimestre=4, ingresos_acumulados=D("100000"), gastos_acumulados=D("20000"))
    assert r.resultado == D("15600.00")
    # Without the flat allowance (not in the simplified method): 20 % x 80,000 = 16,000
    r = _m130(
        trimestre=4,
        ingresos_acumulados=D("100000"),
        gastos_acumulados=D("20000"),
        aplicar_gastos_dificil_justificacion=False,
    )
    assert r.resultado == D("16000.00")


def test_modelo_130_low_income_reduction_carries_within_the_year() -> None:
    q1 = _m130(
        trimestre=1,
        ingresos_acumulados=D("3000"),
        gastos_acumulados=D("1000"),
        retenciones_acumuladas=D("330"),
        rendimiento_neto_ejercicio_anterior=D("8500"),
    )
    # 3,000 - 1,000 = 2,000; flat allowance 100; net 1,900; 20 % = 380; - 330 = 50
    # Previous year 8,500 <= 9,000 -> 100 per quarter: 50 used, 50 left for later quarters
    assert q1.pago_previo_a_minoracion == D("50.00")
    assert q1.minoracion_aplicada == D("50.00")
    assert q1.minoracion_pendiente == D("50.00")
    assert q1.resultado == 0

    q2 = _m130(
        trimestre=2,
        ingresos_acumulados=D("7000"),
        gastos_acumulados=D("2000"),
        retenciones_acumuladas=D("500"),
        pagos_trimestres_anteriores=q1.pago_previo_a_minoracion,
        rendimiento_neto_ejercicio_anterior=D("8500"),
        minoracion_pendiente_trimestres_anteriores=q1.minoracion_pendiente,
    )
    # 7,000 - 2,000 = 5,000; flat allowance 250; net 4,750; 20 % = 950
    # - Q1 (before its reduction) 50 - withholdings 500 = 400; reduction 100 + 50 carried = 150 -> 250
    assert q2.pago_previo_a_minoracion == D("400.00")
    assert q2.minoracion_aplicada == D("150.00")
    assert q2.minoracion_pendiente == 0
    assert q2.resultado == D("250.00")

    def minoracion(anterior: str) -> D:
        return _m130(
            trimestre=1, ingresos_acumulados=D("10000"), rendimiento_neto_ejercicio_anterior=D(anterior)
        ).minoracion_aplicada

    # Table of art. 110.3.c)
    assert [minoracion(x) for x in ("-500", "9000", "9000.01", "10000.01", "11000.01", "12000", "12000.01")] == [
        100,
        100,
        75,
        50,
        25,
        25,
        0,
    ]
    # What is left in the fourth quarter is lost
    q4 = _m130(trimestre=4, rendimiento_neto_ejercicio_anterior=D("0"))
    assert q4.minoracion_pendiente == 0
    assert _avisa(q4, "se pierden")


def test_modelo_130_obligation_for_professionals() -> None:
    base = dict(
        trimestre=1, ingresos_acumulados=D("10000"), gastos_acumulados=D("3000"), retenciones_acumuladas=D("600")
    )
    # 21,000 / 30,000 = 70 % -> "al menos el 70 por ciento": not obliged, nothing to pay
    r = _m130(**base, ingresos_ano_anterior=D("30000"), ingresos_con_retencion_ano_anterior=D("21000"))
    assert r.obligado_a_pago_fraccionado is False
    assert r.resultado == D("730.00")
    assert r.a_ingresar == 0
    # 20,999 / 30,000 = 69.996… % -> obliged
    r = _m130(**base, ingresos_ano_anterior=D("30000"), ingresos_con_retencion_ano_anterior=D("20999"))
    assert r.obligado_a_pago_fraccionado is True
    assert r.a_ingresar == D("730.00")
    # New activity: the share of the period itself counts (8,000 / 10,000 = 80 %)
    r = _m130(**base, inicio_actividad=True, ingresos_con_retencion_acumulados=D("8000"))
    assert r.obligado_a_pago_fraccionado is False
    assert "109.5" in r.motivo_obligacion
    assert _m130(**base, inicio_actividad=True).obligado_a_pago_fraccionado is None
    # A business (non-professional) activity is always obliged and cannot deduct withholdings:
    # 20 % x 6,650 = 1,330
    r = _m130(
        **base,
        actividad_profesional=False,
        ingresos_ano_anterior=D("100"),
        ingresos_con_retencion_ano_anterior=D("100"),
    )
    assert r.obligado_a_pago_fraccionado is True
    assert r.a_ingresar == D("1330.00")
    assert _avisa(r, "No se han restado las retenciones")

    with pytest.raises(ValueError):
        Modelo130Input(ejercicio=2026, trimestre=5)


# --- Modelo 303 ---------------------------------------------------------------


def test_modelo_303_to_pay_with_three_rates_and_carried_balance() -> None:
    r = _m303(
        trimestre=2,
        base_tipo_general=D("10000"),
        base_tipo_reducido=D("2000"),
        base_tipo_superreducido=D("1000"),
        cuotas_soportadas_deducibles=D("900"),
        cuotas_soportadas_bienes_inversion=D("400"),
        cuotas_a_compensar_periodos_anteriores=D("300"),
    )
    # Output: 21 % x 10,000 = 2,100; 10 % x 2,000 = 200; 4 % x 1,000 = 40 -> 2,340
    # Input: 900 + 400 = 1,300 -> difference 1,040; - 300 carried = 740 to pay
    assert r.iva_devengado == D("2340.00")
    assert r.iva_deducible == D("1300.00")
    assert r.diferencia == D("1040.00")
    assert r.compensacion_aplicada == D("300.00")
    assert r.resultado == D("740.00")
    assert r.tipo_resultado == "a_ingresar"
    assert r.saldo_a_compensar == 0 and not r.puede_solicitar_devolucion
    assert _linea(r, "IVA devengado al tipo reducido") == D("200.00")
    assert _avisa(r, "recargo de equivalencia")
    assert _avisa(r, "prorrata")
    assert _avisa(r, "intracomunitarias")
    assert _avisa(r, "4 años")
    assert "BOE-A-1992-28740" in r.fuente


def test_modelo_303_balance_in_favour_rounding_and_zero() -> None:
    # Output 21 % x 1,000 = 210; input 500 -> -290; carried 100 -> -390 to carry forward
    kwargs = dict(
        base_tipo_general=D("1000"),
        cuotas_soportadas_deducibles=D("500"),
        cuotas_a_compensar_periodos_anteriores=D("100"),
    )
    r = _m303(trimestre=1, **kwargs)
    assert r.diferencia == D("-290.00")
    assert r.compensacion_aplicada == 0
    assert r.resultado == D("-390.00")
    assert r.tipo_resultado == "a_compensar"
    assert r.saldo_a_compensar == D("390.00")
    assert not r.puede_solicitar_devolucion
    # Same figures in the last quarter: the refund can be requested (art. 115.uno)
    assert _m303(trimestre=4, **kwargs).puede_solicitar_devolucion

    # The carried balance is larger than the quarter's difference: 210 - 500 = -290 left
    r = _m303(trimestre=1, base_tipo_general=D("1000"), cuotas_a_compensar_periodos_anteriores=D("500"))
    assert r.compensacion_aplicada == D("210.00")
    assert r.saldo_a_compensar == D("290.00")

    # 21 % x 33.33 = 6.9993 -> 7.00
    assert _m303(trimestre=1, base_tipo_general=D("33.33")).resultado == D("7.00")
    r = _m303(trimestre=3)
    assert r.resultado == 0 and r.tipo_resultado == "cero"
