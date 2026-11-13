"""End to end: a realistic household profile, one real-looking statement, every tool and the daily job."""

from __future__ import annotations

from datetime import date
from typing import Any

from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from hacienda_gpt.modulos.suscripciones import MODULO
from hacienda_gpt.modulos.suscripciones.almacen import NOMBRE_HECHO
from tests.modulos.suscripciones.sintetico import FIXTURES


def test_cumple_el_contrato() -> None:
    assert isinstance(MODULO, Modulo)
    assert MODULO.clave == "suscripciones"
    assert all(h.nombre.startswith("suscripciones_") for h in MODULO.hechos)
    assert all(h.spec.name.startswith("suscripciones_") for h in MODULO.herramientas)
    assert {h.spec.name: h.efecto for h in MODULO.herramientas} == {
        "suscripciones_analizar_extracto": "lectura",
        "suscripciones_listado": "lectura",
        "suscripciones_gasto_anual": "lectura",
        "suscripciones_preparar_baja": "prepara",
    }
    assert all(h.sensible for h in MODULO.hechos)
    assert {f.identificador for f in MODULO.fuentes} == {
        "BOE-A-2007-20555",
        "BOE-A-1980-22501",
        "BOE-A-2022-10757",
        "BOE-A-2009-8961",
        "BOE-A-2025-26698",
        "BOE-A-2018-16036",
    }
    for herramienta in MODULO.herramientas:
        esquema = herramienta.spec.input_schema
        assert esquema["type"] == "object" and esquema["additionalProperties"] is False


def test_hogar_completo() -> None:
    perfil: dict[str, Any] = {
        "comunidad_autonoma": "madrid",
        "estado_civil": "casado",
        "edad": 41,
        "num_descendientes": 2,
        "rendimiento_trabajo_bruto": 32000,
    }
    hoy = date(2026, 11, 13)

    def registrar(hechos: Any) -> None:
        for nombre, valor in hechos.items():
            perfil[nombre] = MODULO.hechos[0].validar(valor) if nombre == NOMBRE_HECHO else valor  # type: ignore[misc]

    contexto = ContextoModulo(perfil=perfil, hoy=hoy, case_id="caso-1", registrar=registrar)
    herramientas = {h.spec.name: h for h in MODULO.herramientas}
    contenido = (FIXTURES / "bbva.csv").read_text(encoding="utf-8-sig")
    analisis = herramientas["suscripciones_analizar_extracto"].ejecutar({"contenido": contenido}, contexto)
    assert not analisis.es_error, analisis.contenido
    assert analisis.resumen.startswith("20 servicios recurrentes activos")
    assert perfil["comunidad_autonoma"] == "madrid"  # shared facts untouched

    lectura = ContextoModulo(perfil=perfil, hoy=hoy, case_id="caso-1")
    for nombre, argumentos in [
        ("suscripciones_listado", {}),
        ("suscripciones_gasto_anual", {}),
        ("suscripciones_preparar_baja", {"servicio": "Spotify (PayPal)"}),
    ]:
        resultado = herramientas[nombre].ejecutar(argumentos, lectura)
        assert not resultado.es_error, (nombre, resultado.contenido)
        assert resultado.contenido and resultado.resumen

    avisos = MODULO.avisos(lectura)
    claves = sorted(a.clave.split(":")[1] for a in avisos)
    assert claves == ["duplicado", "duplicado", "duplicado", "renovacion", "subida", "subida"]
    titulos = " | ".join(a.titulo for a in avisos)
    assert "Amazon Prime se renueva el 01/12/2026" in titulos
    assert "Orange ha subido de precio" in titulos and "Mapfre ha subido de precio" in titulos
    assert "Max (HBO): cargo duplicado el 15/09/2026" in titulos
    assert MODULO.avisos(lectura) == avisos  # idempotent

    # A second statement (another format of the same account) does not duplicate services.
    contenido2 = (FIXTURES / "anual.ofx").read_text(encoding="cp1252")
    herramientas["suscripciones_analizar_extracto"].ejecutar({"contenido": contenido2}, contexto)
    assert len(perfil[NOMBRE_HECHO]["servicios"]) == 21
