"""Early-repayment fee caps by regime (Ley 5/2019, Ley 41/2007, Ley 2/1994) and the 2022-2024 suspension."""

from __future__ import annotations

from datetime import date
import decimal

from hacienda_gpt.modulos.hipoteca.comisiones import comision_maxima, tramo_en
from hacienda_gpt.modulos.hipoteca.modelo import Prestamo
from hacienda_gpt.modulos.hipoteca.normativa import cargar_normativa

D = decimal.Decimal

N = cargar_normativa()


def p(**kw) -> Prestamo:
    base = dict(capital_inicial="200000", plazo_meses=360, tipo_inicial="2", fecha_firma="2020-03-01")
    if kw.get("tipo", "variable") != "fijo":
        base.update(tipo="variable", meses_tipo_inicial=12, indice="euribor_12m", diferencial="1")
    base.update(kw)
    return Prestamo.model_validate(base)


def test_ley_5_2019_variable_por_anos():
    v = p()
    assert comision_maxima(v, N, date(2021, 6, 1), D(10000)).importe == D("25.00")  # 0,25 % (opción b)
    c = comision_maxima(v, N, date(2025, 1, 10), D(10000))  # year 4: only option a)
    assert c.porcentaje == D("0.0015") and c.importe == D("15.00") and c.incierta
    sin = comision_maxima(v, N, date(2025, 3, 2), D(10000))  # more than 5 years
    assert sin.importe == 0 and not sin.es_maximo


def test_suspension_2022_2024_en_variables():
    c = comision_maxima(p(), N, date(2023, 6, 1), D(10000))
    assert c.importe == 0 and "BOE-A-2022-19403" in c.citas
    assert comision_maxima(p(), N, date(2022, 11, 23), D(10000)).importe == D("25.00")


def test_pactada_menor_o_ninguna():
    menor = p(comisiones_pactadas=[{"porcentaje": "0.10", "hasta_anos": 3}])
    assert comision_maxima(menor, N, date(2021, 6, 1), D(10000)).importe == D("10.00")
    ninguna = p(comisiones_pactadas=[])
    assert comision_maxima(ninguna, N, date(2021, 6, 1), D(10000)).importe == 0
    mayor = p(comisiones_pactadas=[{"porcentaje": "1"}])
    assert comision_maxima(mayor, N, date(2021, 6, 1), D(10000)).importe == D("25.00")


def test_ley_5_2019_fijo():
    f = p(tipo="fijo", fecha_firma="2020-03-01")
    assert comision_maxima(f, N, date(2026, 1, 1), D(10000)).importe == D("200.00")  # 2 %
    assert comision_maxima(f, N, date(2030, 3, 1), D(10000)).importe == D("150.00")  # 1,5 %


def test_mixto_tramo_fijo_y_variable():
    m = p(tipo="mixto", meses_tipo_inicial=60)
    assert tramo_en(m, date(2024, 1, 1)) == "fijo"
    assert comision_maxima(m, N, date(2024, 12, 1), D(10000)).importe == D("200.00")
    assert tramo_en(m, date(2025, 3, 1)) == "variable"
    assert comision_maxima(m, N, date(2025, 3, 2), D(10000)).importe == 0


def test_cambio_a_fijo_art_23_6():
    joven = p(fecha_firma="2025-01-01")
    assert comision_maxima(joven, N, date(2026, 6, 1), D(100000), "cambio_a_fijo").importe == D("50.00")
    assert comision_maxima(p(), N, date(2026, 6, 1), D(100000), "cambio_a_fijo").importe == 0
    # Applies to old loans too (disposición transitoria primera.3): more than three years -> nothing.
    antiguo = p(fecha_firma="2010-05-01")
    assert comision_maxima(antiguo, N, date(2026, 6, 1), D(100000), "cambio_a_fijo").importe == 0


def test_ley_41_2007():
    v = p(fecha_firma="2012-05-01")
    c = comision_maxima(v, N, date(2026, 6, 1), D(10000))
    assert c.porcentaje == D("0.0025") and "BOE-A-2007-21086" in c.citas
    assert any("riesgo de tipo" in f for f in c.fundamento)
    assert comision_maxima(v, N, date(2015, 6, 1), D(10000)).porcentaje == D("0.005")
    fijo = p(tipo="fijo", fecha_firma="2012-05-01", compensacion_riesgo_tipo="1")
    assert comision_maxima(fijo, N, date(2026, 6, 1), D(10000)).importe == D("125.00")  # 0,25 % + 1 %
    sin_dato = comision_maxima(p(tipo="fijo", fecha_firma="2012-05-01"), N, date(2026, 6, 1), D(10000))
    assert sin_dato.incierta


def test_anteriores_a_2007():
    v = p(fecha_firma="2005-05-01")
    assert comision_maxima(v, N, date(2026, 6, 1), D(10000)).importe == D("100.00")
    assert comision_maxima(v, N, date(2026, 6, 1), D(10000), "subrogacion").importe == D("100.00")
    f = comision_maxima(p(tipo="fijo", fecha_firma="2005-05-01"), N, date(2026, 6, 1), D(10000))
    assert f.importe == 0 and f.incierta
