"""Independent review of the self-employed calculators.

Every expected figure below was worked by hand from the wording of the law
(consolidated texts on boe.es: Ley 35/2006 arts. 30 and 32.2; RD 439/2007
arts. 28 to 30 and 109 to 111; Ley 37/1992 arts. 90, 91, 99 and 115), not from
the code. The quote each case rests on is in the comment above it and is
checked to be in the extracts under ``tests/fixtures/boe_autonomos``.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from hacienda_gpt.tax.actividad import (
    ActividadInput,
    RetencionProfesionalInput,
    calcular_rendimiento_actividad,
    calcular_retencion_profesional,
    cumple_supuesto_d_retencion,
    tipo_retencion_profesional,
)
from hacienda_gpt.tax.autonomos_params import comprobar_evidencias, load_autonomos_params, textos_de_directorio
from hacienda_gpt.tax.iva import Modelo303Input, calcular_modelo_303
from hacienda_gpt.tax.pagos_fraccionados import Modelo130Input, calcular_modelo_130

D = Decimal
EXTRACTOS = Path(__file__).resolve().parents[1] / "fixtures" / "boe_autonomos"
LIRPF, RIRPF, LIVA = "BOE-A-2006-20764", "BOE-A-2007-6820", "BOE-A-1992-28740"


def _act(**kwargs):
    return calcular_rendimiento_actividad(ActividadInput(ejercicio=2026, **kwargs))


def _m130(**kwargs):
    return calcular_modelo_130(Modelo130Input(ejercicio=2026, **kwargs))


def _m303(**kwargs):
    return calcular_modelo_303(Modelo303Input(ejercicio=2026, **kwargs))


def _avisa(result, fragment: str) -> bool:
    return any(fragment in aviso for aviso in result.avisos)


def test_the_sentences_this_review_rests_on_are_in_the_law() -> None:
    textos = {boe_id: " ".join(texto.split()) for boe_id, texto in textos_de_directorio(EXTRACTOS).items()}
    citas = {
        RIRPF: [
            "aplicando el porcentaje del 5 por ciento sobre el rendimiento neto, excluido este concepto, sin que la "
            "cuantía resultante pueda superar 2.000 euros anuales",
            "no resultará de aplicación dicho porcentaje de deducción cuando el contribuyente opte por la aplicación "
            "de la reducción prevista en el artículo 26.1 de este Reglamento",
            "se deducirán los pagos fraccionados que, en relación con estas actividades, habría correspondido "
            "ingresar en los trimestres anteriores del mismo año si no se hubiera aplicado lo dispuesto en la letra "
            "c) del apartado 3 de este artículo",
            "la diferencia podrá deducirse en cualquiera de los siguientes pagos fraccionados correspondientes al "
            "mismo período impositivo cuyo importe positivo lo permita y hasta el límite máximo de dicho importe",
            "al menos el 70 por ciento de los ingresos de la actividad fueron objeto de retención o ingreso a cuenta",
            "los contribuyentes presentarán una declaración negativa",
        ],
        LIRPF: [
            "Cuando los rendimientos netos de actividades económicas sean inferiores a 19.747,5 euros, siempre que "
            "no tengan rentas, excluidas las exentas, distintas de las de actividades económicas superiores a 6.500 "
            "euros",
            "no puede exceder del 30 por ciento de sus rendimientos íntegros declarados",
            "los contribuyentes con rentas no exentas inferiores a 12.000 euros, incluidas las de la propia actividad "
            "económica",
            "conjuntamente con la reducción prevista en el artículo 20 de esta Ley no podrá exceder de 3.700 euros",
            "el saldo resultante no podrá ser negativo",
        ],
        LIVA: [
            "el exceso podrá ser compensado en las declaraciones-liquidaciones posteriores",
            "tendrán derecho a solicitar la devolución del saldo a su favor existente a 31 de diciembre de cada año "
            "en la autoliquidación correspondiente al último período de liquidación de dicho año",
        ],
    }
    for boe_id, frases in citas.items():
        for frase in frases:
            assert frase in textos[boe_id], frase


# --- Net income (art. 30 RIRPF, art. 32.2 LIRPF) -------------------------------


def test_flat_allowance_is_five_percent_of_income_minus_expenses_capped_at_2000() -> None:
    # "5 por ciento sobre el rendimiento neto, excluido este concepto": the base is income minus the
    # reported expenses, not the income.
    # 40,000 - (3,600 + 600 + 4,800 + 1,000) = 30,000; 5 % = 1,500; net 28,500
    r = _act(
        ingresos_integros=D("40000"),
        seguridad_social_autonomo=D("3600"),
        suministros=D("600"),
        alquileres=D("4800"),
        otros_gastos=D("1000"),
    )
    assert r.gastos_deducibles == D("10000.00")
    assert r.gastos_dificil_justificacion == D("1500.00")
    assert r.rendimiento_actividad_neto == D("28500.00")
    assert r.reduccion_art_32_2 == 0
    # Exactly at the cap: 5 % x 40,000 = 2,000. One euro more of margin changes nothing.
    assert _act(ingresos_integros=D("40000")).gastos_dificil_justificacion == D("2000.00")
    assert _act(ingresos_integros=D("40001")).gastos_dificil_justificacion == D("2000.00")
    assert _act(ingresos_integros=D("39999")).gastos_dificil_justificacion == D("1999.95")
    # No positive net income, no allowance: 5,000 - 5,000 = 0
    r = _act(ingresos_integros=D("5000"), otros_gastos=D("5000"))
    assert r.gastos_dificil_justificacion == 0 and r.rendimiento_actividad_neto == 0


def test_dependent_reduction_amounts_between_the_two_thresholds() -> None:
    # Art. 32.2.1.º: 2,000 + [6,498 - 1.14 x (net - 14,047.5)] when the net is between 14,047.5 and 19,747.5
    # and other income does not exceed 6,500. Incompatible with the flat allowance (art. 32.2.2.º a).
    # 20,000 - 4,000 = 16,000 (expenses 20 % <= 30 %); no flat allowance
    # additional = 6,498 - 1.14 x 1,952.5 = 6,498 - 2,225.85 = 4,272.15; total 6,272.15 -> 9,727.85
    kwargs = dict(
        ingresos_integros=D("20000"),
        otros_gastos=D("4000"),
        cumple_requisitos_reduccion_dependiente=True,
        otras_rentas_no_exentas=D("0"),
    )
    r = _act(**kwargs)
    assert r.gastos_dificil_justificacion == 0
    assert r.rendimiento_neto == D("16000.00")
    assert r.reduccion_art_32_2 == D("6272.15")
    assert r.rendimiento_actividad_neto == D("9727.85")
    assert r.reduccion_aplicada == "art. 32.2.1.º LIRPF"
    # Disability adds 3,500 (7,750 with help from third parties, reduced mobility or 65 %)
    assert _act(**kwargs, discapacidad=True).reduccion_art_32_2 == D("9772.15")
    assert _act(**kwargs, discapacidad_ampliada=True).reduccion_art_32_2 == D("14022.15")
    # Other income of exactly 6,500 is not "superior a 6.500": the additional amount stays; 6,500.01 loses it
    assert _act(**{**kwargs, "otras_rentas_no_exentas": D("6500")}).reduccion_art_32_2 == D("6272.15")
    assert _act(**{**kwargs, "otras_rentas_no_exentas": D("6500.01")}).reduccion_art_32_2 == D("2000.00")
    # Other income unknown: only the 2,000 that does not depend on it
    r = _act(**{**kwargs, "otras_rentas_no_exentas": None})
    assert r.reduccion_art_32_2 == D("2000.00")
    assert _avisa(r, "no se aplica la reducción adicional")


def test_dependent_reduction_edges() -> None:
    base = dict(cumple_requisitos_reduccion_dependiente=True, otras_rentas_no_exentas=D("0"))
    # Net exactly 19,747.5 is not "inferior a 19.747,5": 2,000 only
    assert _act(ingresos_integros=D("19747.5"), **base).reduccion_art_32_2 == D("2000.00")
    # One cent below: 6,498 - 1.14 x 5,699.99 = 0.0114 -> 2,000.01
    assert _act(ingresos_integros=D("19747.49"), **base).reduccion_art_32_2 == D("2000.01")
    # Net exactly 14,047.5: the full 6,498 -> 8,498 -> 5,549.50
    r = _act(ingresos_integros=D("14047.5"), **base)
    assert r.reduccion_art_32_2 == D("8498.00") and r.rendimiento_actividad_neto == D("5549.50")
    # "El saldo resultante no podrá ser negativo": 5,000 of net, 8,498 of reduction -> 0
    r = _act(ingresos_integros=D("5000"), **base)
    assert r.reduccion_art_32_2 == D("5000.00") and r.rendimiento_actividad_neto == 0
    # Expenses of exactly 30 % of the income do not "exceed" it: 20,000 - 6,000 = 14,000 -> 8,498 -> 5,502
    r = _act(ingresos_integros=D("20000"), otros_gastos=D("6000"), **base)
    assert r.reduccion_aplicada == "art. 32.2.1.º LIRPF" and r.rendimiento_actividad_neto == D("5502.00")
    # One cent more: requirement c) fails, so the general rules come back: flat allowance
    # 5 % x 13,999.99 = 699.9995 -> 700.00; net 13,299.99; income >= 12,000 -> no art. 32.2.3.º reduction
    r = _act(ingresos_integros=D("20000"), otros_gastos=D("6000.01"), **base)
    assert r.reduccion_aplicada is None
    assert r.gastos_dificil_justificacion == D("700.00")
    assert r.rendimiento_actividad_neto == D("13299.99")
    assert _avisa(r, "superan el 30 %")
    # A loss: nothing to reduce and nothing invented
    r = _act(ingresos_integros=D("1000"), otros_gastos=D("300"), alquileres=D("900"), **base)
    assert r.rendimiento_actividad_neto == D("-200.00") and r.reduccion_art_32_2 == 0


def test_low_income_reduction_formula_limits_and_joint_cap() -> None:
    # Art. 32.2.3.º. 10,000 - 1,000 = 9,000; flat allowance 450; net 8,550; other income 1,000 -> 9,550
    # 1,620 - 0.405 x (9,550 - 8,000) = 1,620 - 627.75 = 992.25 -> 7,557.75
    kwargs = dict(ingresos_integros=D("10000"), otros_gastos=D("1000"))
    r = _act(**kwargs, otras_rentas_no_exentas=D("1000"))
    assert r.rendimiento_neto == D("8550.00")
    assert r.reduccion_art_32_2 == D("992.25")
    assert r.rendimiento_actividad_neto == D("7557.75")
    assert r.reduccion_aplicada == "art. 32.2.3.º LIRPF"
    # Joint cap with the art. 20 reduction: 3,700 - 3,000 = 700
    r = _act(**kwargs, otras_rentas_no_exentas=D("1000"), reduccion_rendimientos_trabajo=D("3000"))
    assert r.reduccion_art_32_2 == D("700.00")
    assert (
        _act(**kwargs, otras_rentas_no_exentas=D("1000"), reduccion_rendimientos_trabajo=D("3700")).reduccion_art_32_2
        == 0
    )
    # Income of exactly 12,000 is not "inferior a 12.000": 8,550 + 3,450
    assert _act(**kwargs, otras_rentas_no_exentas=D("3450")).reduccion_art_32_2 == 0
    # 11,999.99: 1,620 - 0.405 x 3,999.99 = 0.00405 -> 0.00
    assert _act(**kwargs, otras_rentas_no_exentas=D("3449.99")).reduccion_art_32_2 == 0
    # Sum of exactly 8,000 ("igual o inferior a 8.000"): 6,000 - 300 = 5,700; + 2,300 -> 1,620 -> 4,080
    r = _act(ingresos_integros=D("6000"), otras_rentas_no_exentas=D("2300"))
    assert r.reduccion_art_32_2 == D("1620.00") and r.rendimiento_actividad_neto == D("4080.00")
    # Net below the reduction: 1,000 - 50 = 950 -> 0, never negative
    r = _act(ingresos_integros=D("1000"), otras_rentas_no_exentas=D("0"))
    assert r.reduccion_art_32_2 == D("950.00") and r.rendimiento_actividad_neto == 0
    # Other income unknown: not applied, and said so
    r = _act(**kwargs)
    assert r.reduccion_art_32_2 == 0 and _avisa(r, "no se ha calculado porque no se han indicado las demás rentas")


# --- Modelo 130 (arts. 109 to 111 RIRPF) ---------------------------------------


def test_modelo_130_full_year_order_of_subtractions_and_carry_over() -> None:
    anterior = D("9500")  # previous year's net income: "Entre 9.000,01 y 10.000" -> 75 per quarter
    # Q1: 12,000 - 3,000 = 9,000; flat 450; net 8,550; 20 % = 1,710; - withholdings 900 = 810; - 75 = 735
    q1 = _m130(
        trimestre=1,
        ingresos_acumulados=D("12000"),
        gastos_acumulados=D("3000"),
        retenciones_acumuladas=D("900"),
        rendimiento_neto_ejercicio_anterior=anterior,
    )
    assert (q1.rendimiento_neto_acumulado, q1.pago_sobre_rendimiento) == (D("8550.00"), D("1710.00"))
    assert (q1.pago_previo_a_minoracion, q1.minoracion_aplicada, q1.resultado) == (D("810.00"), 75, D("735.00"))
    assert q1.minoracion_pendiente == 0

    # Q2 (cumulative): 20,000 - 8,000 = 12,000; flat 600; net 11,400; 20 % = 2,280
    # - what Q1 would have paid WITHOUT its reduction (810, not 735) = 1,470; - withholdings 1,500 = -30 -> 0
    # the 75 of the quarter cannot be used: carried
    q2 = _m130(
        trimestre=2,
        ingresos_acumulados=D("20000"),
        gastos_acumulados=D("8000"),
        retenciones_acumuladas=D("1500"),
        pagos_trimestres_anteriores=q1.pago_previo_a_minoracion,
        rendimiento_neto_ejercicio_anterior=anterior,
        minoracion_pendiente_trimestres_anteriores=q1.minoracion_pendiente,
    )
    assert (q2.pago_previo_a_minoracion, q2.minoracion_aplicada, q2.resultado) == (0, 0, 0)
    assert q2.minoracion_pendiente == D("75.00")
    assert _avisa(q2, "declaración negativa")

    # Q3: 50,000 - 9,000 = 41,000; flat 5 % = 2,050 -> 2,000; net 39,000; 20 % = 7,800
    # - previous quarters 810 + 0 = 6,990; - withholdings 3,000 = 3,990; - (75 + 75 carried) = 3,840
    q3 = _m130(
        trimestre=3,
        ingresos_acumulados=D("50000"),
        gastos_acumulados=D("9000"),
        retenciones_acumuladas=D("3000"),
        pagos_trimestres_anteriores=q1.pago_previo_a_minoracion + q2.pago_previo_a_minoracion,
        rendimiento_neto_ejercicio_anterior=anterior,
        minoracion_pendiente_trimestres_anteriores=q2.minoracion_pendiente,
    )
    assert q3.rendimiento_neto_acumulado == D("39000.00")
    assert (q3.pago_previo_a_minoracion, q3.minoracion_aplicada, q3.resultado) == (D("3990.00"), 150, D("3840.00"))

    # Q4: the year ends lower (credit notes): 45,000 - 12,000 = 33,000; flat 1,650; net 31,350; 20 % = 6,270
    # - previous 810 + 0 + 3,990 = 4,800 -> 1,470; - withholdings 3,400 -> -1,930 -> 0; the 75 is lost
    q4 = _m130(
        trimestre=4,
        ingresos_acumulados=D("45000"),
        gastos_acumulados=D("12000"),
        retenciones_acumuladas=D("3400"),
        pagos_trimestres_anteriores=D("4800"),
        rendimiento_neto_ejercicio_anterior=anterior,
    )
    assert q4.resultado == 0 and q4.a_ingresar == 0
    assert q4.minoracion_aplicada == 0 and q4.minoracion_pendiente == 0
    assert _avisa(q4, "se pierden")
    # The order of the lines is the order of the regulation
    conceptos = [line.concepto for line in q3.lineas]
    orden = [
        next(i for i, c in enumerate(conceptos) if c.startswith(prefix))
        for prefix in (
            "20 % del rendimiento neto",
            "Pagos fraccionados de los trimestres anteriores",
            "Retenciones e ingresos a cuenta",
            "Pago fraccionado antes de la minoración",
            "Minoración por rendimientos netos bajos",
            "Resultado del pago fraccionado",
        )
    ]
    assert orden == sorted(orden)


def test_modelo_130_seventy_percent_exemption_edges() -> None:
    base = dict(trimestre=2, ingresos_acumulados=D("8000"), gastos_acumulados=D("0"))
    # 20 % x (8,000 - 400) = 1,520
    # "al menos el 70 por ciento": 7,000 / 10,000 is enough; 6,999.99 is not
    r = _m130(**base, ingresos_ano_anterior=D("10000"), ingresos_con_retencion_ano_anterior=D("7000"))
    assert r.obligado_a_pago_fraccionado is False and r.a_ingresar == 0 and r.resultado == D("1520.00")
    r = _m130(**base, ingresos_ano_anterior=D("10000"), ingresos_con_retencion_ano_anterior=D("6999.99"))
    assert r.obligado_a_pago_fraccionado is True and r.a_ingresar == D("1520.00")
    # Start of activity: the period of the payment itself (art. 109.5): 5,600 / 8,000 = 70 %
    r = _m130(**base, inicio_actividad=True, ingresos_con_retencion_acumulados=D("5600"))
    assert r.obligado_a_pago_fraccionado is False
    r = _m130(**base, inicio_actividad=True, ingresos_con_retencion_acumulados=D("5599"))
    assert r.obligado_a_pago_fraccionado is True


def test_modelo_130_rejects_figures_that_cannot_be() -> None:
    # "los trimestres anteriores del mismo año": none before the first
    with pytest.raises(ValueError, match="primer trimestre"):
        Modelo130Input(ejercicio=2026, trimestre=1, pagos_trimestres_anteriores=D("100"))
    with pytest.raises(ValueError, match="primer trimestre"):
        Modelo130Input(ejercicio=2026, trimestre=1, minoracion_pendiente_trimestres_anteriores=D("50"))
    # More income with withholding than income: the 70 % test would be meaningless
    with pytest.raises(ValueError, match="no pueden superar"):
        Modelo130Input(
            ejercicio=2026,
            trimestre=2,
            ingresos_ano_anterior=D("1000"),
            ingresos_con_retencion_ano_anterior=D("1500"),
        )
    with pytest.raises(ValueError, match="no pueden superar"):
        Modelo130Input(
            ejercicio=2026,
            trimestre=2,
            ingresos_acumulados=D("1000"),
            inicio_actividad=True,
            ingresos_con_retencion_acumulados=D("1000.01"),
        )
    assert Modelo130Input(ejercicio=2026, trimestre=1).pagos_trimestres_anteriores == 0


# --- Modelo 303 (arts. 90, 91, 99 and 115 LIVA) --------------------------------


def test_modelo_303_compensation_and_refund_only_in_the_last_quarter() -> None:
    # Output: 21 % x 10,000 = 2,100; 10 % x 1,000 = 100; 4 % x 500 = 20 -> 2,220
    # Input: 700 + 300 = 1,000 -> 1,220; carried 2,000 -> -780: 1,220 used, 780 still in favour
    kwargs = dict(
        base_tipo_general=D("10000"),
        base_tipo_reducido=D("1000"),
        base_tipo_superreducido=D("500"),
        cuotas_soportadas_deducibles=D("700"),
        cuotas_soportadas_bienes_inversion=D("300"),
        cuotas_a_compensar_periodos_anteriores=D("2000"),
    )
    r = _m303(trimestre=1, **kwargs)
    assert (r.iva_devengado, r.iva_deducible, r.diferencia) == (D("2220.00"), D("1000.00"), D("1220.00"))
    assert (r.compensacion_aplicada, r.resultado, r.saldo_a_compensar) == (D("1220.00"), D("-780.00"), D("780.00"))
    assert r.tipo_resultado == "a_compensar"
    for trimestre in (1, 2, 3):
        assert not _m303(trimestre=trimestre, **kwargs).puede_solicitar_devolucion
    q4 = _m303(trimestre=4, **kwargs)
    assert q4.puede_solicitar_devolucion and _avisa(q4, "puedes pedir la devolución")
    # In the last quarter with something to pay there is nothing to refund
    r = _m303(trimestre=4, base_tipo_general=D("1000"))
    assert r.resultado == D("210.00") and not r.puede_solicitar_devolucion and r.saldo_a_compensar == 0
    # The carried balance exactly offsets the quarter: zero, nothing left
    r = _m303(trimestre=4, base_tipo_general=D("1000"), cuotas_a_compensar_periodos_anteriores=D("210"))
    assert r.tipo_resultado == "cero" and r.saldo_a_compensar == 0 and not r.puede_solicitar_devolucion


# --- Follow-up: 2025, art. 32.1 and 32.3, Ceuta and Melilla, withholding ----------------------


def test_2025_parameters_are_the_wording_in_force_in_2025() -> None:
    # Extracts built from the boe.es open-data XML: for each article, the last version whose date
    # of entry into force is not after 31-12-2025.
    p25, p26 = load_autonomos_params(2025), load_autonomos_params(2026)
    textos = textos_de_directorio(EXTRACTOS / "2025")
    assert set(textos) == set(p25.fuentes_principales)
    assert comprobar_evidencias(p25, textos) == []
    assert all(grupo.fuente.verificado for grupo in p25.grupos().values())
    assert all(fp.consolidado_a == "2025-12-31" for fp in p25.fuentes_principales.values())
    # The provision for Ceuta is of 2026 only; every other figure is the same in both years.
    assert p25.gastos_dificil_justificacion_ceuta is None and p26.gastos_dificil_justificacion_ceuta is not None
    assert "sexagésima cuarta" not in textos[LIRPF]
    excluir = {"fuente", "evidencia", "avisos"}
    for nombre, grupo in p25.grupos().items():
        assert grupo.model_dump(exclude=excluir) == getattr(p26, nombre).model_dump(exclude=excluir), nombre
    # 40,000 - 10,000 = 30,000; 5 % = 1,500 -> 28,500, in 2025 as in 2026
    r = calcular_rendimiento_actividad(
        ActividadInput(ejercicio=2025, ingresos_integros=D("40000"), otros_gastos=D("10000"))
    )
    assert r.rendimiento_actividad_neto == D("28500.00")
    assert "vigente a 31-12-2025" in r.fuente
    q = calcular_modelo_130(
        Modelo130Input(ejercicio=2025, trimestre=1, ingresos_acumulados=D("12000"), gastos_acumulados=D("3000"))
    )
    assert q.resultado == D("1710.00")  # 20 % x 8,550
    assert (
        calcular_modelo_303(Modelo303Input(ejercicio=2025, trimestre=1, base_tipo_general=D("1000"))).resultado == 210
    )


def test_irregular_income_reduction_of_art_32_1() -> None:
    # "se reducirán en un 30 por ciento"; "no podrá superar el importe de 300.000 euros anuales"
    # 60,000 - 10,000 = 50,000; flat 5 % = 2,500 -> 2,000; net 48,000; 30 % x 20,000 = 6,000 -> 42,000
    r = _act(ingresos_integros=D("60000"), otros_gastos=D("10000"), rendimientos_irregulares=D("20000"))
    assert r.rendimiento_neto == D("48000.00")
    assert (r.reduccion_art_32_1, r.reduccion_art_32_2, r.reduccion_art_32_3) == (D("6000.00"), 0, 0)
    assert r.rendimiento_actividad_neto == D("42000.00")
    assert _avisa(r, "art. 25 RIRPF") and _avisa(r, "de forma regular o habitual")
    # Base capped at 300,000: 500,000 - 2,000 = 498,000; 30 % x 300,000 = 90,000 -> 408,000
    r = _act(ingresos_integros=D("500000"), rendimientos_irregulares=D("400000"))
    assert r.reduccion_art_32_1 == D("90000.00") and r.rendimiento_actividad_neto == D("408000.00")
    # Not asked for, not applied, and said so
    r = _act(ingresos_integros=D("60000"))
    assert r.reduccion_art_32_1 == 0 and _avisa(r, "No se ha aplicado la del 30 % por rendimientos irregulares")
    with pytest.raises(ValueError, match="no pueden superar los ingresos"):
        ActividadInput(ejercicio=2026, ingresos_integros=D("100"), rendimientos_irregulares=D("101"))


def test_new_activity_reduction_of_art_32_3_comes_after_the_other_two() -> None:
    # "podrán reducir en un 20 por ciento el rendimiento neto positivo declarado [...], minorado en su caso por
    # las reducciones previstas en los apartados 1 y 2 anteriores"
    # Net 48,000 -> 20 % = 9,600 -> 38,400
    r = _act(ingresos_integros=D("60000"), otros_gastos=D("10000"), reduccion_inicio_actividad=True)
    assert r.reduccion_art_32_3 == D("9600.00") and r.rendimiento_actividad_neto == D("38400.00")
    # After art. 32.1: 48,000 - 6,000 = 42,000; 20 % = 8,400 -> 33,600
    r = _act(
        ingresos_integros=D("60000"),
        otros_gastos=D("10000"),
        rendimientos_irregulares=D("20000"),
        reduccion_inicio_actividad=True,
    )
    assert (r.reduccion_art_32_1, r.reduccion_art_32_3) == (D("6000.00"), D("8400.00"))
    assert r.rendimiento_actividad_neto == D("33600.00")
    # After art. 32.2.3.º: net 8,550, other income 1,000 -> 992.25 -> 7,557.75; 20 % = 1,511.55 -> 6,046.20
    r = _act(
        ingresos_integros=D("10000"),
        otros_gastos=D("1000"),
        otras_rentas_no_exentas=D("1000"),
        reduccion_inicio_actividad=True,
    )
    assert (r.reduccion_art_32_2, r.reduccion_art_32_3) == (D("992.25"), D("1511.55"))
    assert r.rendimiento_actividad_neto == D("6046.20")
    # Base capped at 100,000: net 498,000 -> 20,000 -> 478,000
    r = _act(ingresos_integros=D("500000"), reduccion_inicio_actividad=True)
    assert r.reduccion_art_32_3 == D("20000.00") and r.rendimiento_actividad_neto == D("478000.00")
    # "rendimiento neto positivo": a loss is not reduced
    r = _act(ingresos_integros=D("1000"), otros_gastos=D("3000"), reduccion_inicio_actividad=True)
    assert r.reduccion_art_32_3 == 0 and r.rendimiento_actividad_neto == D("-2000.00")
    assert _avisa(r, "primer ejercicio con rendimiento neto positivo o el siguiente")


def test_ceuta_flat_allowance_of_2026_and_what_stays_general() -> None:
    # DA 64.ª.1 LIRPF: "será, durante el período impositivo 2026, del 10 por ciento"; the 2,000 cap is untouched.
    kwargs = dict(ingresos_integros=D("30000"), otros_gastos=D("10000"))
    # 20,000 x 10 % = 2,000 -> 18,000 (general: 5 % = 1,000 -> 19,000)
    r = _act(**kwargs, ceuta_melilla="ceuta")
    assert r.gastos_dificil_justificacion == D("2000.00") and r.rendimiento_actividad_neto == D("18000.00")
    assert _avisa(r, "ni este cálculo ni el simulador de IRPF la aplican")
    assert _act(**kwargs).rendimiento_actividad_neto == D("19000.00")
    assert _avisa(_act(**kwargs), "Si la actividad se desarrolla en Ceuta")
    # 12,000 x 10 % = 1,200
    assert _act(ingresos_integros=D("12000"), ceuta_melilla="ceuta").gastos_dificil_justificacion == D("1200.00")
    # The cap still holds: 40,000 x 10 % = 4,000 -> 2,000
    assert _act(ingresos_integros=D("40000"), ceuta_melilla="ceuta").gastos_dificil_justificacion == D("2000.00")
    # Melilla is not in the provision; 2025 has no such provision
    r = _act(**kwargs, ceuta_melilla="melilla")
    assert r.rendimiento_actividad_neto == D("19000.00") and _avisa(r, "solo para Ceuta")
    r = calcular_rendimiento_actividad(ActividadInput(ejercicio=2025, ceuta_melilla="ceuta", **kwargs))
    assert r.rendimiento_actividad_neto == D("19000.00")


def test_modelo_130_in_ceuta_and_melilla() -> None:
    # Art. 110.2 RIRPF: "se reducirán en un 60 por ciento" -> 20 % x 0.4 = 8 %.
    # DA 64.ª.3 LIRPF: the 10 % only in payments whose filing period starts a month after 3-9-2026: the third
    # quarter's starts on 1 October (art. 111.1 RIRPF), so only the fourth quarter.
    kwargs = dict(ingresos_acumulados=D("30000"), gastos_acumulados=D("10000"))
    # Q3: flat 5 % x 20,000 = 1,000; net 19,000; 8 % = 1,520
    q3 = _m130(trimestre=3, ceuta_melilla="ceuta", **kwargs)
    assert q3.rendimiento_neto_acumulado == D("19000.00") and q3.resultado == D("1520.00")
    assert _avisa(q3, "solo cuenta en los pagos fraccionados desde el trimestre 4")
    # Q4: flat 10 % = 2,000; net 18,000; 8 % = 1,440
    q4 = _m130(trimestre=4, ceuta_melilla="ceuta", **kwargs)
    assert q4.rendimiento_neto_acumulado == D("18000.00") and q4.resultado == D("1440.00")
    # Melilla: the 8 %, never the 10 %
    assert _m130(trimestre=4, ceuta_melilla="melilla", **kwargs).resultado == D("1520.00")
    # Elsewhere: 20 % x 19,000 = 3,800
    r = _m130(trimestre=4, **kwargs)
    assert r.resultado == D("3800.00") and _avisa(r, "el porcentaje se reduce en un 60 %")
    # 2025: 8 %, general flat allowance in every quarter
    r = calcular_modelo_130(Modelo130Input(ejercicio=2025, trimestre=4, ceuta_melilla="ceuta", **kwargs))
    assert r.resultado == D("1520.00")


def test_modelo_130_annual_cap_is_not_prorated_and_last_year_at_or_below_zero() -> None:
    # Art. 30.2.ª RIRPF: "sin que la cuantía resultante pueda superar 2.000 euros anuales"; art. 110.1.a): the
    # payment is on the net income "desde el primer día del año". No rule splits the cap by quarter.
    # Q1: 50,000 x 5 % = 2,500 -> 2,000 (not 500); net 48,000; 20 % = 9,600
    q1 = _m130(trimestre=1, ingresos_acumulados=D("50000"))
    assert q1.rendimiento_neto_acumulado == D("48000.00") and q1.resultado == D("9600.00")
    assert _avisa(q1, "sin prorratear")
    # Below the cap nothing is said: 10,000 x 5 % = 500
    assert not _avisa(_m130(trimestre=1, ingresos_acumulados=D("10000")), "sin prorratear")
    # In the fourth quarter the cumulative figure is the annual one
    assert not _avisa(_m130(trimestre=4, ingresos_acumulados=D("50000")), "sin prorratear")

    # "Igual o inferior a 9.000 -> 100": zero and a loss are in that bracket by its literal wording
    for anterior in ("0", "-3000"):
        r = _m130(trimestre=1, ingresos_acumulados=D("10000"), rendimiento_neto_ejercicio_anterior=D(anterior))
        # 9,500 x 20 % = 1,900; - 100 = 1,800
        assert r.minoracion_aplicada == 100 and r.resultado == D("1800.00")
        assert _avisa(r, "nulo o negativo") and _avisa(r, "no regula expresamente")
    r = _m130(trimestre=1, ingresos_acumulados=D("10000"), rendimiento_neto_ejercicio_anterior=D("0.01"))
    assert r.minoracion_aplicada == 100 and not _avisa(r, "nulo o negativo")


def test_withholding_rates_of_art_95_1() -> None:
    p = load_autonomos_params(2026)
    assert tipo_retencion_profesional(p) == D("0.15")
    for supuesto in ("recaudador_municipal", "mediador_seguros", "delegado_loterias", "actividad_ingresos_reducidos"):
        assert tipo_retencion_profesional(p, supuesto=supuesto) == D("0.07")
    # "Estos porcentajes se reducirán en un 60 por ciento": 15 % -> 6 %, 7 % -> 2.8 %
    assert tipo_retencion_profesional(p, ceuta_melilla=True) == D("0.06")
    assert tipo_retencion_profesional(p, inicio_actividad=True, ceuta_melilla=True) == D("0.028")
    # Letter d): "inferior a 15.000 euros y represente más del 75 por ciento"
    assert cumple_supuesto_d_retencion(p, D("14999.99"), D("19000"))  # 78.9 %
    assert not cumple_supuesto_d_retencion(p, D("15000"), D("15000"))  # not below 15,000
    assert not cumple_supuesto_d_retencion(p, D("12000"), D("16000"))  # exactly 75 % is not "más del"
    assert cumple_supuesto_d_retencion(p, D("12000.01"), D("16000"))


def _ret(**kwargs):
    return calcular_retencion_profesional(RetencionProfesionalInput(ejercicio=2026, **kwargs))


def test_withholding_calculator_checks_letter_d_and_says_what_it_does_not() -> None:
    general = _ret()
    assert (general.tipo, general.tipo_porcentaje, general.cumple_supuesto_d) == (D("0.15"), D("15.00"), None)
    assert not general.parametros_no_verificados and RIRPF in general.fuente
    # Start-up rate cut by 60 % (Ceuta or Melilla): 7 % -> 2.8 %, which only survives in percentage points.
    reducido = _ret(inicio_actividad=True, ceuta_melilla=True)
    assert reducido.tipo_porcentaje == D("2.80") and _avisa(reducido, "comunicarlo por escrito al pagador")
    assert _avisa(reducido, "reducido en un 60 %")
    # Letter d) with its figures: 15.000 is not "inferior a 15.000 euros" -> the rate without that case.
    fuera = _ret(
        supuesto="actividad_ingresos_reducidos",
        integros_actividades_letra_d=D(15000),
        integros_actividades_y_trabajo=D(15000),
    )
    assert fuera.cumple_supuesto_d is False and fuera.tipo == D("0.15") and _avisa(fuera, "No se cumplen las cifras")
    # Same, but in the start-up period: the start-up rate still applies.
    assert _ret(supuesto="actividad_ingresos_reducidos", inicio_actividad=True, integros_actividades_letra_d=D(15000), integros_actividades_y_trabajo=D(15000)).tipo == D("0.07")  # fmt: skip
    dentro = _ret(
        supuesto="actividad_ingresos_reducidos",
        integros_actividades_letra_d=D("12000.01"),
        integros_actividades_y_trabajo=D(16000),
    )
    assert (
        dentro.cumple_supuesto_d is True
        and dentro.tipo == D("0.07")
        and _avisa(dentro, "grupos y agrupaciones del IAE")
    )
    sin_cifras = _ret(supuesto="actividad_ingresos_reducidos")
    assert sin_cifras.tipo == D("0.07") and _avisa(sin_cifras, "sin comprobar sus cifras")
    # The figures of letter d) are reported even when that case is not claimed; the rate does not change.
    informativo = _ret(integros_actividades_letra_d=D(1000), integros_actividades_y_trabajo=D(1000))
    assert informativo.cumple_supuesto_d is True and informativo.tipo == D("0.15")
    assert _ret(supuesto="mediador_seguros").tipo == D("0.07")
    with pytest.raises(ValueError, match="las dos cifras"):
        _ret(integros_actividades_letra_d=D(1000))
    with pytest.raises(ValueError, match="no pueden superarlos"):
        _ret(integros_actividades_letra_d=D(2000), integros_actividades_y_trabajo=D(1000))


def test_modelo_303_says_where_vat_does_not_apply() -> None:
    assert _avisa(_m303(trimestre=1), "no se aplica en Canarias, Ceuta ni Melilla")
