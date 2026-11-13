"""Tools and proactive notices of the mortgage module, with a synthetic index series (no network)."""

from __future__ import annotations

from datetime import date
import decimal

from hipoteca_util import serie_basica
import pytest

from hacienda_gpt.modulos.contrato import ContextoModulo
from hacienda_gpt.modulos.hipoteca import herramientas as h
from hacienda_gpt.modulos.hipoteca.avisos import avisos_hipoteca
from hacienda_gpt.modulos.hipoteca.normativa import cargar_normativa

D = decimal.Decimal

VARIABLE = {
    "capital_inicial": "150000",
    "fecha_firma": "2021-01-15",
    "plazo_meses": 300,
    "tipo": "variable",
    "tipo_inicial": "1,5",
    "meses_tipo_inicial": 12,
    "indice": "euribor_12m",
    "diferencial": "0,9",
    "revisiones_conocidas": [
        {"fecha": "2022-01-15", "tipo": "0.9"},
        {"fecha": "2023-01-15", "tipo": "3.9"},
        {"fecha": "2024-01-15", "tipo": "3.4"},
    ],
    "adquisicion_antes_2013": False,
}
FIJO = {
    "capital_inicial": "100000",
    "fecha_firma": "2020-01-15",
    "plazo_meses": 300,
    "tipo": "fijo",
    "tipo_inicial": "3",
}


@pytest.fixture(autouse=True)
def _serie(monkeypatch):
    monkeypatch.setattr(h, "cargar_serie", serie_basica)


def ctx(prestamo, hoy=date(2025, 12, 20), **extra) -> ContextoModulo:
    return ContextoModulo(perfil={"hipoteca_prestamo": prestamo, **extra}, hoy=hoy)


def herramienta(nombre):
    return next(x for x in h.HERRAMIENTAS if x.spec.name == nombre)


def test_sin_datos_es_un_error_legible():
    r = herramienta("hipoteca_cuadro").ejecutar({}, ContextoModulo(perfil={}, hoy=date(2026, 1, 1)))
    assert r.es_error and "hipoteca_prestamo" in r.contenido
    r = herramienta("hipoteca_cuadro").ejecutar({}, ctx({**FIJO, "plazo_meses": -1}))
    assert r.es_error and "no son válidos" in r.contenido
    r = herramienta("hipoteca_cuadro").ejecutar({"fecha": "ayer"}, ctx(FIJO))
    assert r.es_error and "AAAA-MM-DD" in r.contenido


def test_cuadro_fijo():
    r = herramienta("hipoteca_cuadro").ejecutar({"fecha": "2020-03-20", "cuotas": 2, "ejercicio": 2021}, ctx(FIJO))
    assert not r.es_error
    assert "Capital pendiente: 99.551,02 €" in r.contenido
    assert "Próxima cuota: n.º 3, 15/04/2020: 474,21 €" in r.contenido
    assert "Año 2021: cuotas por 5.690,52 €" in r.contenido
    assert "Redondeo" in r.contenido and "disposición transitoria 18.ª LIRPF" in r.contenido


def test_cuadro_variable_usa_revisiones_del_banco_y_boe():
    r = herramienta("hipoteca_cuadro").ejecutar({}, ctx(VARIABLE))
    # Review 15/01/2025: Euríbor November 2024 (3,000) + 0,9.
    assert "Tipo vigente: 3,9 % desde el 15/01/2025" in r.contenido
    assert "BOE-A-2024-1200" in r.contenido
    assert any("BOE-A-2024-1200" in c for c in r.citas)


def test_revision_estimada_y_sensibilidad():
    r = herramienta("hipoteca_revision").ejecutar({"desplazamientos_pp": [1]}, ctx(VARIABLE))
    assert not r.es_error
    # Review 15/01/2026 needs November 2025 (-0,500), published 02/12/2025: known on 20/12/2025.
    assert "Índice que corresponde (ya publicado)" in r.contenido
    assert "= 0,4 %" in r.contenido
    assert "+1,00 puntos" in r.contenido
    fijo = herramienta("hipoteca_revision").ejecutar({}, ctx(FIJO))
    assert not fijo.es_error and "tipo fijo" in fijo.contenido


def test_revision_aun_no_publicada():
    r = herramienta("hipoteca_revision").ejecutar({}, ctx(VARIABLE, hoy=date(2025, 11, 20)))
    assert "todavía no está publicado" in r.contenido and "octubre de 2025" in r.contenido


def test_amortizar_compara_plazo_cuota_y_alternativa():
    r = herramienta("hipoteca_amortizar").ejecutar({"importe": 20000, "rentabilidad_alternativa_pct": 2}, ctx(FIJO))
    assert not r.es_error
    texto = r.contenido
    assert "Comisión por amortización anticipada" in texto and "400,00 €" in texto  # 2 % fixed, first 10 years
    assert "A) Reducir plazo" in texto and "B) Reducir cuota" in texto
    assert "No es una recomendación de inversión" in texto
    assert "Con estos supuestos sale mayor: reducir plazo." in texto  # 2 % < 3 % loan rate
    alta = herramienta("hipoteca_amortizar").ejecutar({"importe": 20000, "rentabilidad_alternativa_pct": 6}, ctx(FIJO))
    assert "Con estos supuestos sale mayor: invertir." in alta.contenido
    error = herramienta("hipoteca_amortizar").ejecutar({}, ctx(FIJO))
    assert error.es_error


def test_amortizar_ahorro_coincide_con_el_cuadro():
    entorno = h._Entorno(ctx(FIJO), serie=serie_basica())
    base = entorno.cuadro()
    r = herramienta("hipoteca_amortizar").ejecutar({"importe": 10000, "fecha": "2026-01-15"}, ctx(FIJO))
    from hacienda_gpt.modulos.hipoteca.modelo import AmortizacionRealizada

    plazo = entorno.cuadro(amortizaciones=(AmortizacionRealizada(fecha=date(2026, 1, 15), importe=D(10000)),))
    ahorro = base.intereses_desde(date(2026, 1, 15)) - plazo.intereses_desde(date(2026, 1, 15))
    assert f"Intereses que te ahorras: {h.fmt(ahorro)} €" in r.contenido


def test_cambiar_banco_subrogacion_a_fijo():
    args = {
        "modalidad": "subrogacion",
        "oferta": {"tipo": "fijo", "tipo_interes": 2.2},
        "gastos": {"tasacion": 300, "notaria": 0},
        "vinculaciones_coste_anual_nueva": 200,
    }
    r = herramienta("hipoteca_cambiar_banco").ejecutar(args, ctx(VARIABLE))
    assert not r.es_error, r.contenido
    assert "Comisión al banco actual: 0,00 €" in r.contenido  # art. 23.6, more than 3 years
    assert "Tasación: 300,00 €" in r.contenido
    assert "enervar" in r.contenido and "AJD" in r.contenido
    assert any("BOE-A-1994-7556" in c for c in r.citas)


def test_cambiar_banco_novacion_ampliando_plazo():
    args = {
        "modalidad": "novacion",
        "oferta": {"tipo": "variable", "tipo_interes": 3, "diferencial": 0.6, "plazo_meses": 400},
        "gastos": {"comision_novacion": 500},
    }
    r = herramienta("hipoteca_cambiar_banco").ejecutar(args, ctx(VARIABLE))
    assert not r.es_error, r.contenido
    assert "art. 10 Ley 2/1994" in r.contenido and "lo supera" in r.contenido
    assert "los plazos no coinciden" in r.contenido


def test_cambiar_banco_errores():
    r = herramienta("hipoteca_cambiar_banco").ejecutar({"oferta": {"tipo": "fijo"}}, ctx(VARIABLE))
    assert r.es_error and "tipo_interes" in r.contenido
    r = herramienta("hipoteca_cambiar_banco").ejecutar(
        {"oferta": {"tipo": "fijo", "tipo_interes": 2}, "gastos": {"cafe": 3}}, ctx(VARIABLE)
    )
    assert r.es_error and "cafe" in r.contenido


def test_pagos_del_ejercicio_para_el_modulo_fiscal():
    pagos = h.pagos_del_ejercicio(ctx(FIJO), 2021)
    assert pagos is not None and pagos["capital"] + pagos["intereses"] == D("5690.52")
    assert h.pagos_del_ejercicio(ContextoModulo(perfil={}, hoy=date(2026, 1, 1)), 2021) is None


# --- Proactive notices ---------------------------------------------------------------------------


def test_aviso_de_revision_proxima_idempotente():
    contexto = ctx(VARIABLE, hoy=date(2025, 12, 20))
    a1 = avisos_hipoteca(contexto, serie_basica())
    a2 = avisos_hipoteca(contexto, serie_basica())
    assert [a.clave for a in a1] == [a.clave for a in a2]
    revision = next(a for a in a1 if a.clave.startswith("hipoteca:revision:"))
    assert revision.clave == "hipoteca:revision:2026-01-15:BOE-A-2025-1200"
    assert revision.sustituye_prefijo == "hipoteca:revision:2026-01-15:"
    assert revision.vence == date(2026, 1, 15) and revision.importe is not None


def test_sin_aviso_de_revision_lejana():
    avisos = avisos_hipoteca(ctx(VARIABLE, hoy=date(2025, 6, 1)), serie_basica())
    assert not [a for a in avisos if a.clave.startswith("hipoteca:revision:")]


def test_aviso_de_nuevo_indice_que_mueve_la_cuota():
    # On 05/11/2025 the review of 15/01/2026 is projected with October 2025 (2,000), published 02/11/2025;
    # the month before (2024-12 in this series) was 2,800: the estimate moves by more than 1 €.
    contexto = ctx(VARIABLE, hoy=date(2025, 11, 5), hipoteca_umbral_aviso=1)
    avisos = avisos_hipoteca(contexto, serie_basica())
    indice = [a for a in avisos if a.clave.startswith("hipoteca:indice:")]
    assert len(indice) == 1 and indice[0].clave == "hipoteca:indice:BOE-A-2025-1100"
    assert indice[0].gravedad == "info"  # the estimate goes down
    sin = avisos_hipoteca(ctx(VARIABLE, hoy=date(2025, 11, 5), hipoteca_umbral_aviso=100000), serie_basica())
    assert not [a for a in sin if a.clave.startswith("hipoteca:indice:")]


def test_aviso_fin_de_comision():
    prestamo = {**VARIABLE, "fecha_firma": "2023-01-10", "revisiones_conocidas": []}
    avisos = avisos_hipoteca(ctx(prestamo, hoy=date(2025, 12, 1)), serie_basica(), cargar_normativa())
    comision = [a for a in avisos if a.clave.startswith("hipoteca:comision:")]
    assert len(comision) == 1 and comision[0].clave == "hipoteca:comision:2026-01-10"
    assert "0,25 %" in comision[0].cuerpo and "0,15 %" in comision[0].cuerpo


def test_sin_hipoteca_no_hay_avisos():
    assert avisos_hipoteca(ContextoModulo(perfil={}, hoy=date(2026, 1, 1))) == []
    assert avisos_hipoteca(ctx({"capital_inicial": "x"})) == []
