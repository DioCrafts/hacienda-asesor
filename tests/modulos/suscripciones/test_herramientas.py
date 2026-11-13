"""Tools of the suscripciones module: outputs, storage, errors and cancellation drafts."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from hacienda_gpt.modulos.contrato import ContextoModulo, ResultadoHerramienta
from hacienda_gpt.modulos.suscripciones import MODULO
from hacienda_gpt.modulos.suscripciones.almacen import NOMBRE_HECHO, fusionar, leer, validar_servicios
from hacienda_gpt.modulos.suscripciones.modelos import ServiciosGuardados
from tests.modulos.suscripciones.sintetico import FIXTURES

HOY = date(2026, 11, 13)
HERRAMIENTAS = {h.spec.name: h for h in MODULO.herramientas}


def _ejecutar(nombre: str, argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    return HERRAMIENTAS[nombre].ejecutar(argumentos, contexto)


@pytest.fixture
def perfil_con_servicios() -> dict[str, Any]:
    perfil: dict[str, Any] = {}
    contexto = ContextoModulo(perfil=perfil, hoy=HOY, registrar=perfil.update)
    contenido = (FIXTURES / "anual.n43").read_text(encoding="latin-1")
    resultado = _ejecutar("suscripciones_analizar_extracto", {"contenido": contenido}, contexto)
    assert not resultado.es_error
    return perfil


def test_analizar_texto_guarda_solo_lo_derivado() -> None:
    perfil: dict[str, Any] = {}
    contexto = ContextoModulo(perfil=perfil, hoy=HOY, registrar=perfil.update)
    contenido = (FIXTURES / "anual_camt053.xml").read_text(encoding="utf-8")
    resultado = _ejecutar("suscripciones_analizar_extracto", {"contenido": contenido, "formato": "auto"}, contexto)
    assert not resultado.es_error
    assert "Servicios recurrentes activos: 20" in resultado.contenido
    assert "Subida de precio el 05/04/2026: 12,99 € → 13,99 € (+7,70 %)" in resultado.contenido
    assert "Cargo duplicado el 15/09/2026" in resultado.contenido
    assert "Prueba convertida en pago" in resultado.contenido
    assert "cuenta ····1332" in resultado.contenido
    assert "BOE-A-2007-20555" in " ".join(resultado.citas)
    assert set(perfil) == {NOMBRE_HECHO}
    guardado = perfil[NOMBRE_HECHO]
    texto = str(guardado)
    # Nothing raw: no account number, no concept text, no unrelated merchants, no balances.
    for prohibido in ("ES9121000418450200051332", "5540XXXXXXXX1234", "MERCADONA", "GLOVO", "NOMINA", "BF-000123"):
        assert prohibido not in texto
    assert len(guardado["servicios"]) == 21
    assert validar_servicios(guardado) == guardado


def test_analizar_xlsx_en_base64_y_sin_guardar() -> None:
    perfil: dict[str, Any] = {}
    contexto = ContextoModulo(perfil=perfil, hoy=HOY, registrar=perfil.update)
    datos = base64.b64encode((FIXTURES / "openbank.xlsx").read_bytes()).decode()
    resultado = _ejecutar("suscripciones_analizar_extracto", {"contenido_base64": datos, "guardar": False}, contexto)
    assert not resultado.es_error and "Columnas usadas" in resultado.contenido
    assert "no se ha guardado (lo has pedido así)" in resultado.contenido
    assert perfil == {}


def test_analizar_sin_permiso_de_escritura() -> None:
    contexto = ContextoModulo(perfil={}, hoy=HOY)
    contenido = (FIXTURES / "santander.csv").read_text(encoding="cp1252")
    resultado = _ejecutar("suscripciones_analizar_extracto", {"contenido": contenido}, contexto)
    assert "no puede escribir" in resultado.contenido


@pytest.mark.parametrize(
    ("argumentos", "mensaje"),
    [
        ({}, "No se ha recibido"),
        ({"contenido": "hola;adiós\n1;2"}, "cabecera"),
        ({"contenido": "x", "formato": "camt053"}, "XML"),
        ({"contenido_base64": base64.b64encode(b"%PDF-1.4").decode()}, "PDF"),
    ],
)
def test_analizar_errores_legibles(argumentos: dict[str, Any], mensaje: str) -> None:
    resultado = _ejecutar("suscripciones_analizar_extracto", argumentos, ContextoModulo(perfil={}, hoy=HOY))
    assert resultado.es_error and resultado.contenido.startswith("No se ha podido leer el extracto")
    assert mensaje in resultado.contenido


def test_fusion_conserva_otros_extractos(perfil_con_servicios: dict[str, Any]) -> None:
    anterior = leer(perfil_con_servicios)
    assert anterior is not None
    otro = anterior.servicios[0].model_copy(update={"id": "srv-0123456789", "etiqueta": "Otro banco"})
    fusion = fusionar(anterior, [otro], HOY, None)
    assert len(fusion.servicios) == len(anterior.servicios) + 1
    # An older statement never overwrites newer data of the same service.
    viejo = anterior.servicios[1].model_copy(update={"ultima_fecha": date(2020, 1, 1), "importe": 1})
    assert fusionar(anterior, [viejo], HOY, None).servicios == anterior.servicios


def test_validar_rechaza_datos_corruptos() -> None:
    with pytest.raises(ValueError, match="JSON"):
        validar_servicios("{no json")
    with pytest.raises(ValueError, match="formato"):
        validar_servicios({"actualizado_el": "2026-01-01", "servicios": [{"id": "x"}]})
    assert ServiciosGuardados.model_validate(validar_servicios({"actualizado_el": "2026-01-01"})).servicios == []


def test_listado(perfil_con_servicios: dict[str, Any]) -> None:
    contexto = ContextoModulo(perfil=perfil_con_servicios, hoy=HOY)
    activos = _ejecutar("suscripciones_listado", {}, contexto)
    assert activos.contenido.startswith("20 servicio(s)") and "Filmin" not in activos.contenido
    todos = _ejecutar("suscripciones_listado", {"incluir_inactivas": True}, contexto)
    assert "Filmin" in todos.contenido
    seguros = _ejecutar("suscripciones_listado", {"categoria": "seguros"}, contexto)
    assert "Mapfre" in seguros.contenido and "Netflix" not in seguros.contenido
    vacio = _ejecutar("suscripciones_listado", {}, ContextoModulo(perfil={}, hoy=HOY))
    assert not vacio.es_error and "Aún no hay servicios" in vacio.contenido
    roto = _ejecutar("suscripciones_listado", {}, ContextoModulo(perfil={NOMBRE_HECHO: "{x"}, hoy=HOY))
    assert roto.es_error


def test_gasto_anual(perfil_con_servicios: dict[str, Any]) -> None:
    resultado = _ejecutar("suscripciones_gasto_anual", {}, ContextoModulo(perfil=perfil_con_servicios, hoy=HOY))
    texto = resultado.contenido
    assert "Gasto anual estimado en servicios recurrentes: 18.073,16 € (1.506,10 € al mes)." in texto
    assert "Streaming de vídeo: 527,64 € al año" in texto
    assert "Kiosko y Más: 1,99 € × 52 (semanal) = 103,48 €" in texto
    assert "Iberdrola: 65,69 € × 6 (cada dos meses) = 394,14 € — importe variable" in texto
    assert "Amazon Prime: 49,90 € × 1 (anual) = 49,90 € — confianza baja" in texto


def _baja(perfil: dict[str, Any], **argumentos: Any) -> ResultadoHerramienta:
    return _ejecutar("suscripciones_preparar_baja", argumentos, ContextoModulo(perfil=perfil, hoy=HOY))


def test_preparar_baja_general(perfil_con_servicios: dict[str, Any]) -> None:
    resultado = _baja(perfil_con_servicios, servicio="Netflix", canal="carta")
    assert HERRAMIENTAS["suscripciones_preparar_baja"].efecto == "prepara"
    texto = resultado.contenido
    assert texto.startswith("BORRADOR (no se ha enviado nada)")
    assert "en la misma forma en que lo celebró" in texto and "(art. 62.3, párrafo segundo" in texto
    assert "[Firma]" in texto and "tienen hasta el 28/12/2026" in texto
    assert any("BOE-A-2025-26698" in c for c in resultado.citas)


def test_preparar_baja_seguro_calcula_el_preaviso(perfil_con_servicios: dict[str, Any]) -> None:
    texto = _baja(perfil_con_servicios, servicio="Mapfre").contenido
    assert "Oposición a la prórroga de la póliza Mapfre" in texto
    assert "a más tardar el 20/09/2027" in texto
    tarde = _baja(perfil_con_servicios, servicio="Mutua Madrileña", fecha_vencimiento="2026-12-01").contenido
    assert "a más tardar el 01/11/2026" in tarde and "ya ha pasado" in tarde


def test_preparar_baja_telecom_subida(perfil_con_servicios: dict[str, Any]) -> None:
    texto = _baja(perfil_con_servicios, servicio="Orange", motivo="subida_precio").contenido
    assert "ejerzo mi derecho a rescindir el contrato sin coste alguno" in texto
    assert "a partir del 17/11/2026" in texto  # Friday 13 + 2 working days
    assert "al menos con un mes de antelación" in texto


def test_preparar_baja_desistimiento(perfil_con_servicios: dict[str, Any]) -> None:
    caducado = _baja(perfil_con_servicios, servicio="DAZN", motivo="desistimiento")
    assert caducado.es_error and "terminó el 17/07/2026" in caducado.contenido
    vigente = _baja(perfil_con_servicios, servicio="DAZN", motivo="desistimiento", fecha_contratacion="2026-11-05")
    assert not vigente.es_error and "hasta el 19/11/2026" in vigente.contenido
    assert "catorce días naturales" in vigente.contenido


def test_preparar_baja_cobro_tras_baja(perfil_con_servicios: dict[str, Any]) -> None:
    texto = _baja(perfil_con_servicios, servicio="Basic-Fit", motivo="cobro_tras_baja").contenido
    assert "durante 8 semanas" in texto and "hasta el 26/11/2026" in texto
    assert "(5 días si es un cobro indebido)" in texto


def test_preparar_baja_casos_sin_carta_y_errores(perfil_con_servicios: dict[str, Any]) -> None:
    assert "arrendamiento" in _baja(perfil_con_servicios, servicio="Alquiler").contenido
    assert "préstamo" in _baja(perfil_con_servicios, servicio="Préstamo").contenido
    ambiguo = _baja(perfil_con_servicios, servicio="Apple")
    assert ambiguo.es_error and "Indica el id" in ambiguo.contenido
    desconocido = _baja(perfil_con_servicios, servicio="Gimnasio del barrio")
    assert desconocido.es_error and "sector" in desconocido.contenido
    libre = _baja(perfil_con_servicios, servicio="Gimnasio del barrio", sector="general")
    assert not libre.es_error and "«Gimnasio del barrio»" in libre.contenido
    fecha_mala = _baja(perfil_con_servicios, servicio="Netflix", fecha_contratacion="05/11/2026")
    assert fecha_mala.es_error


def test_ninguna_excepcion_llega_al_agente() -> None:
    resultado = _ejecutar("suscripciones_listado", {}, ContextoModulo(perfil=None, hoy=HOY))  # type: ignore[arg-type]
    assert isinstance(resultado, ResultadoHerramienta)
