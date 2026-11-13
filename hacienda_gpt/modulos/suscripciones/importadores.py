"""Entry point of the statement importers: detects the format and dispatches to its reader.

Formats: AEB Norma 43 (``norma43``), CSV (``csv``), XLSX (``xlsx``), OFX/QFX (``ofx``/``qfx``) and ISO 20022
camt.053/052/054 (``camt053``). Everything happens in memory; nothing read here is written anywhere.
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from hacienda_gpt.modulos.suscripciones.camt import es_camt, leer_camt
from hacienda_gpt.modulos.suscripciones.lectura import TAMANO_MAXIMO, decodificar
from hacienda_gpt.modulos.suscripciones.modelos import ErrorImportacion, Extracto
from hacienda_gpt.modulos.suscripciones.norma43 import es_norma43, leer_norma43
from hacienda_gpt.modulos.suscripciones.ofx import codificacion_declarada, es_ofx, leer_ofx
from hacienda_gpt.modulos.suscripciones.tablas import filas_csv, filas_xlsx, tabla_a_extracto

Formato = Literal["auto", "norma43", "csv", "xlsx", "ofx", "qfx", "camt053"]
FORMATOS: tuple[str, ...] = Formato.__args__  # type: ignore[attr-defined]


def bytes_de_entrada(contenido: str | None, contenido_base64: str | None) -> bytes:
    """The file as bytes, from its text or from base64 (binary formats such as XLSX)."""
    if contenido_base64:
        try:
            datos = base64.b64decode(contenido_base64, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ErrorImportacion("El contenido en base64 no es válido.") from exc
    elif contenido:
        datos = contenido.encode("utf-8")
    else:
        raise ErrorImportacion("No se ha recibido el contenido del extracto.")
    if len(datos) > TAMANO_MAXIMO:
        raise ErrorImportacion("El fichero es demasiado grande (más de 20 MB). Exporta un periodo más corto.")
    return datos


def detectar_formato(datos: bytes) -> str:
    if datos.startswith(b"PK\x03\x04"):
        return "xlsx"
    if datos.startswith(b"\xd0\xcf\x11\xe0"):
        raise ErrorImportacion(
            "Es un Excel antiguo (.xls), que no se puede leer. Expórtalo de nuevo como XLSX o CSV, o usa Norma 43."
        )
    if datos.startswith(b"%PDF"):
        raise ErrorImportacion(
            "Los extractos en PDF no se leen en esta fase. Descarga el extracto en Norma 43, CSV, Excel, OFX o camt.053."
        )
    texto = decodificar(datos[:20000])
    if es_ofx(texto):
        return "qfx" if "INTU.BID" in texto.upper() else "ofx"
    if es_camt(texto):
        return "camt053"
    if es_norma43(texto):
        return "norma43"
    return "csv"


def importar(
    datos: bytes,
    formato: str = "auto",
    banco: str | None = None,
    mapeo_columnas: dict[str, str] | None = None,
    importes_positivos_son: str | None = None,
) -> Extracto:
    """Parse a statement. Raises :class:`ErrorImportacion` with a Spanish message on any problem."""
    if formato not in FORMATOS:
        raise ErrorImportacion(f"Formato desconocido «{formato}». Usa uno de: {', '.join(FORMATOS)}.")
    real = detectar_formato(datos) if formato == "auto" else formato
    if real == "xlsx":
        if not datos.startswith(b"PK"):
            raise ErrorImportacion("El fichero no es un XLSX (envíalo en contenido_base64).")
        return tabla_a_extracto(filas_xlsx(datos), "xlsx", banco, mapeo_columnas, importes_positivos_son)
    if real in {"ofx", "qfx"}:
        return leer_ofx(decodificar(datos, codificacion_declarada(datos)), real)
    texto = decodificar(datos)
    if real == "camt053":
        return leer_camt(texto)
    if real == "norma43":
        return leer_norma43(texto)
    return tabla_a_extracto(filas_csv(texto), "csv", banco, mapeo_columnas, importes_positivos_son)
