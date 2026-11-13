"""Schedules checked by hand (and with an independent float implementation of the same conventions).

Hand check of the variable case: 12.000 € at 2 % for 24 months, i = 0,02/12 = 0,0016667;
(1+i)^24 = 1,040777, so the instalment is 12.000 × i / (1 − 1/1,040777) = 20 / 0,039179 = 510,48 €.
After 12 instalments 6.059,96 € remain; at 4 % (Euríbor 3,000 + 1) i = 0,0033333, (1+i)^12 = 1,040742,
instalment = 6.059,96 × i / (1 − 1/1,040742) = 20,1999 / 0,039147 = 516,01 €.
"""

from __future__ import annotations

from datetime import date
import decimal

from hipoteca_util import serie_basica, serie_sintetica
import pytest

from hacienda_gpt.modulos.hipoteca.calculo import (
    Supuestos,
    calcular_cuadro,
    comparar_con_banco,
    cuota_francesa,
    cuotas_necesarias,
    fechas_revision,
    fmt,
    fmt_pct,
    mes_menos,
    sumar_meses,
)
from hacienda_gpt.modulos.hipoteca.modelo import AmortizacionRealizada, Prestamo, validar_prestamo

D = decimal.Decimal

HOY = date(2026, 11, 13)


def prestamo(**kw) -> Prestamo:
    return Prestamo.model_validate(kw)


FIJO = dict(capital_inicial="100000", fecha_firma="2020-01-15", plazo_meses=300, tipo="fijo", tipo_inicial="3")
VARIABLE = dict(
    capital_inicial="12000",
    fecha_firma="2024-01-10",
    plazo_meses=24,
    tipo="variable",
    tipo_inicial="2",
    meses_tipo_inicial=12,
    indice="euribor_12m",
    diferencial="1",
)
MIXTO = dict(
    capital_inicial="60000",
    fecha_firma="2020-01-15",
    plazo_meses=120,
    tipo="mixto",
    tipo_inicial="2,5",
    meses_tipo_inicial=36,
    indice="euribor_12m",
    diferencial="0,9",
)


def fila(cuadro, n):
    f = cuadro.filas[n - 1]
    return (f.cuota, f.intereses, f.amortizacion, f.capital_pendiente)


def test_cuota_francesa_conocida():
    # 100.000 € at 3 % over 25 years: the textbook instalment.
    assert cuota_francesa(D(100000), D(3), 300) == D("474.21")
    assert cuota_francesa(D(1200), D(0), 12) == D("100.00")
    assert cuota_francesa(D(1200), D(6), 4, periodicidad_meses=3) == D("311.33")  # 18 / (1 - 1,015^-4)


def test_fijo_frances():
    c = calcular_cuadro(prestamo(**FIJO), serie_basica(), Supuestos(hoy=HOY))
    assert len(c.filas) == 300
    assert fila(c, 1) == (D("474.21"), D("250.00"), D("224.21"), D("99775.79"))
    assert fila(c, 2) == (D("474.21"), D("249.44"), D("224.77"), D("99551.02"))
    # The last instalment repays what rounding left.
    assert fila(c, 300) == (D("474.70"), D("1.18"), D("473.52"), D("0.00"))
    assert c.intereses_totales() == D("42263.49")
    assert c.filas[0].fecha == date(2020, 2, 15) and c.fecha_fin == date(2045, 1, 15)
    assert not c.hay_estimados()


def test_variable_con_revision():
    c = calcular_cuadro(prestamo(**VARIABLE), serie_basica(), Supuestos(hoy=HOY))
    assert fila(c, 1) == (D("510.48"), D("20.00"), D("490.48"), D("11509.52"))
    assert fila(c, 12) == (D("510.48"), D("10.93"), D("499.55"), D("6059.96"))
    # Review 10/01/2025 with the Euríbor of November 2024 (two months before): 3,000 + 1 = 4 %.
    assert fila(c, 13) == (D("516.01"), D("20.20"), D("495.81"), D("5564.15"))
    assert fila(c, 24) == (D("515.95"), D("1.71"), D("514.24"), D("0.00"))
    assert c.intereses_totales() == D("317.82")
    revision = c.cambios[1]
    assert (revision.fecha, revision.tipo, revision.origen) == (date(2025, 1, 10), D(4), "boe")
    assert revision.dato is not None and revision.dato.mes == "2024-11"


def test_mixto_tramo_fijo_y_revisiones():
    c = calcular_cuadro(prestamo(**MIXTO), serie_basica(), Supuestos(hoy=HOY))
    assert fila(c, 1) == (D("565.62"), D("125.00"), D("440.62"), D("59559.38"))
    assert fila(c, 36) == (D("565.62"), D("91.71"), D("473.91"), D("43545.48"))
    # 15/01/2023: Euríbor nov-2022 3,000 + 0,9; 15/01/2024: nov-2023 2,500 + 0,9.
    assert fila(c, 37) == (D("593.21"), D("141.52"), D("451.69"), D("43093.79"))
    assert fila(c, 48) == (D("593.21"), D("125.11"), D("468.10"), D("38027.27"))
    assert fila(c, 49) == (D("584.60"), D("107.74"), D("476.86"), D("37550.41"))
    assert [ch.tipo for ch in c.cambios[:3]] == [D("2.5"), D("3.9"), D("3.4")]


def test_revision_futura_se_estima_con_el_ultimo_publicado():
    serie = serie_basica()
    p = prestamo(**{**MIXTO, "fecha_firma": "2024-03-15", "meses_tipo_inicial": 24})
    c = calcular_cuadro(p, serie, Supuestos(hoy=date(2025, 12, 20)))
    primera = c.cambios[1]
    # Review 15/03/2026 needs January 2026: not published on 20/12/2025 -> last one (November 2025).
    assert primera.origen == "estimado" and primera.dato is not None and primera.dato.mes == "2025-11"
    assert primera.tipo == D("0.4")  # -0,500 + 0,9
    assert c.hay_estimados()
    subida = calcular_cuadro(p, serie, Supuestos(hoy=date(2025, 12, 20), desplazamiento_pp=D(1)))
    assert subida.cambios[1].tipo == D("1.4")


def test_tipo_nunca_negativo_suelo_techo_y_redondeo():
    serie = serie_sintetica({"2024-11": "-0.750"})
    base = {**VARIABLE, "diferencial": "0.5"}
    assert calcular_cuadro(prestamo(**base), serie, Supuestos(hoy=HOY)).cambios[1].tipo == D(0)
    con_suelo = calcular_cuadro(prestamo(**base, tipo_minimo="1.5"), serie, Supuestos(hoy=HOY))
    assert con_suelo.cambios[1].tipo == D("1.5")
    serie2 = serie_sintetica({"2024-11": "3.070"})
    redondeo = calcular_cuadro(prestamo(**base, redondeo_tipo_multiplo="0.125"), serie2, Supuestos(hoy=HOY))
    assert redondeo.cambios[1].tipo == D("3.625")  # 3,570 to the nearest 1/8
    techo = calcular_cuadro(prestamo(**base, tipo_maximo="3"), serie2, Supuestos(hoy=HOY))
    assert techo.cambios[1].tipo == D(3)


def test_bonificaciones_reducen_el_diferencial():
    p = prestamo(**VARIABLE, bonificaciones=[{"concepto": "nómina", "reduccion_diferencial": "0.25"}])
    assert calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY)).cambios[1].tipo == D("3.75")
    perdida = prestamo(
        **VARIABLE, bonificaciones=[{"concepto": "nómina", "reduccion_diferencial": "0.25", "cumplida": False}]
    )
    assert calcular_cuadro(perdida, serie_basica(), Supuestos(hoy=HOY)).cambios[1].tipo == D(4)


def test_revision_conocida_manda_sobre_el_indice():
    p = prestamo(**VARIABLE, revisiones_conocidas=[{"fecha": "2025-01-10", "tipo": "4,1"}])
    c = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY))
    assert c.cambios[1].origen == "banco" and c.cambios[1].tipo == D("4.1")
    assert c.filas[12].cuota == cuota_francesa(D("6059.96"), D("4.1"), 12)


def test_regla_ultimo_publicado():
    p = prestamo(**VARIABLE, indice_regla="ultimo_publicado")
    c = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY))
    # On 10/01/2025 the last value published was December 2024 (published 02/01/2025).
    assert c.cambios[1].dato is not None and c.cambios[1].dato.mes == "2024-12"
    assert c.cambios[1].tipo == D("3.8")


def test_amortizacion_reduciendo_cuota_y_plazo():
    p = prestamo(**FIJO)
    base = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY))
    pendiente = base.filas[11].capital_pendiente
    extra_cuota = AmortizacionRealizada(fecha=base.filas[11].fecha, importe=D(10000), modalidad="cuota")
    c = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY, amortizaciones=(extra_cuota,)))
    assert c.filas[11].amortizacion_extra == D(10000)
    assert c.filas[11].capital_pendiente == pendiente - 10000
    assert c.filas[12].cuota == cuota_francesa(pendiente - 10000, D(3), 288)
    assert len(c.filas) == 300
    extra_plazo = AmortizacionRealizada(fecha=base.filas[11].fecha, importe=D(10000), modalidad="plazo")
    c2 = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY, amortizaciones=(extra_plazo,)))
    n = cuotas_necesarias(pendiente - 10000, D(3), D("474.21"))
    assert len(c2.filas) == 12 + n
    assert c2.filas[12].cuota == cuota_francesa(pendiente - 10000, D(3), n)
    assert c2.intereses_totales() < c.intereses_totales() < base.intereses_totales()


def test_amortizacion_realizada_del_contrato_y_cancelacion_total():
    p = prestamo(**FIJO, amortizaciones_realizadas=[{"fecha": "2021-01-20", "importe": "200000"}])
    c = calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY))
    assert c.filas[-1].capital_pendiente == 0 and len(c.filas) == 12


def test_capital_pendiente_y_pagos_del_ano():
    c = calcular_cuadro(prestamo(**FIJO), serie_basica(), Supuestos(hoy=HOY))
    assert c.capital_pendiente(date(2020, 2, 14)) == D(100000)
    assert c.capital_pendiente(date(2020, 3, 20)) == D("99551.02")
    capital, intereses = c.pagos_del_ano(2021)
    assert capital + intereses == D("474.21") * 12
    assert c.proxima(date(2020, 3, 20)).numero == 3


def test_comparacion_con_banco():
    p = prestamo(
        **FIJO, comprobacion_banco=[{"fecha": "2020-03-15", "cuota": "474,21", "capital_pendiente": "99551,00"}]
    )
    lineas = comparar_con_banco(calcular_cuadro(p, serie_basica(), Supuestos(hoy=HOY)))
    assert "diferencia 0,00 €" in lineas[0]
    assert "diferencia 0,02 €" in lineas[1]


def test_utilidades_de_fechas_y_formato():
    assert sumar_meses(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert sumar_meses(date(2024, 1, 31), 2, 31) == date(2024, 3, 31)
    assert mes_menos(date(2026, 1, 15), 2) == "2025-11"
    assert fmt(D("1234567.891")) == "1.234.567,89"
    assert fmt_pct(D("3.045")) == "3,045 %" and fmt_pct(D("2")) == "2 %"
    p = prestamo(**MIXTO)
    assert fechas_revision(p)[:2] == [date(2023, 1, 15), date(2024, 1, 15)]


def test_validacion_del_hecho():
    canon = validar_prestamo({**VARIABLE, "capital_inicial": "12.000,00"})
    assert canon["capital_inicial"] == "12000.00"
    with pytest.raises(ValueError, match="índice y diferencial"):
        validar_prestamo({**VARIABLE, "diferencial": None})
    with pytest.raises(ValueError, match="meses_tipo_inicial|tipo inicial"):
        validar_prestamo({**VARIABLE, "meses_tipo_inicial": None})
    with pytest.raises(ValueError, match="múltiplo"):
        validar_prestamo({**FIJO, "periodicidad_meses": 12, "plazo_meses": 301})
    with pytest.raises(ValueError, match="objeto JSON"):
        validar_prestamo("hipoteca")
    with pytest.raises(ValueError, match="Datos de la hipoteca no válidos"):
        validar_prestamo({**FIJO, "campo_raro": 1})
