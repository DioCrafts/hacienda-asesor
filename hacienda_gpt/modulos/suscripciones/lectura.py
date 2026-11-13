"""Low-level reading helpers shared by every statement importer: text decoding, amounts and dates.

Spanish bank exports mix encodings (UTF-8, UTF-8 with BOM, Windows-1252, ISO-8859-1), decimal commas
and dots, thousands separators, trailing or bracketed signs and several date layouts. Every helper here
is strict: a value it cannot read raises :class:`ErrorImportacion` (or returns ``None`` where the caller
decides), never a guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from hacienda_gpt.modulos.suscripciones.modelos import ErrorImportacion

# Largest statement accepted, uncompressed (a year of a busy account is well under 5 MB).
TAMANO_MAXIMO = 20 * 1024 * 1024


def decodificar(datos: bytes, declarada: str | None = None) -> str:
    """Bytes -> text: the declared encoding if valid, else UTF-8 (with or without BOM), else Windows-1252."""
    if len(datos) > TAMANO_MAXIMO:
        raise ErrorImportacion("El fichero es demasiado grande (más de 20 MB). Exporta un periodo más corto.")
    candidatas = [declarada] if declarada else []
    candidatas += ["utf-8-sig", "cp1252", "latin-1"]
    for codificacion in candidatas:
        try:
            return datos.decode(codificacion)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ErrorImportacion("No se ha podido leer la codificación del fichero.")  # pragma: no cover (latin-1)


def sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


# --- Amounts --------------------------------------------------------------------------------------

_MENOS = "-−‒–—"
_LIMPIA_IMPORTE = re.compile(r"(?i)\s|€|eur(?:os?)?\b| | ")


def separador_decimal(valores: Iterable[str]) -> str:
    """Decide the decimal separator of a whole column: ',' (Spanish default) or '.'.

    A value with both separators settles it (the last one is the decimal). Otherwise a dot followed by
    exactly one or two digits at the end, with no comma anywhere in the column, means a decimal dot.
    """
    vistos_coma = vistos_punto_decimal = False
    for bruto in valores:
        valor = _LIMPIA_IMPORTE.sub("", bruto)
        if "," in valor and "." in valor:
            return "," if valor.rfind(",") > valor.rfind(".") else "."
        if "," in valor:
            vistos_coma = True
        elif re.search(r"\.\d{1,2}\)?[" + _MENOS + r"]?$", valor):
            vistos_punto_decimal = True
    if vistos_punto_decimal and not vistos_coma:
        return "."
    return ","


def leer_importe(bruto: str, decimal: str = ",") -> Decimal:
    """'-1.234,56 €' -> Decimal('-1234.56'); '(12,99)' and '12,99-' are negative too."""
    valor = _LIMPIA_IMPORTE.sub("", bruto.strip())
    if not valor:
        raise ErrorImportacion("Importe vacío.")
    negativo = False
    if valor.startswith("(") and valor.endswith(")"):
        negativo, valor = True, valor[1:-1]
    if valor and valor[0] in _MENOS:
        negativo, valor = not negativo, valor[1:]
    elif valor and valor[-1] in _MENOS:
        negativo, valor = not negativo, valor[:-1]
    if valor.startswith("+"):
        valor = valor[1:]
    miles = "." if decimal == "," else ","
    valor = valor.replace(miles, "").replace(decimal, ".")
    if not re.fullmatch(r"\d+(\.\d+)?", valor):
        raise ErrorImportacion(f"Importe ilegible: «{bruto.strip()}».")
    try:
        numero = Decimal(valor)
    except InvalidOperation as exc:  # pragma: no cover (regex above)
        raise ErrorImportacion(f"Importe ilegible: «{bruto.strip()}».") from exc
    return -numero if negativo else numero


# --- Dates ----------------------------------------------------------------------------------------

_FECHA_DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})(?:[ T].*)?$")
_FECHA_YMD = re.compile(r"^(\d{4})[/\-.]?(\d{2})[/\-.]?(\d{2})(?:[ T].*)?$")
EPOCA_EXCEL = date(1899, 12, 30)


def _ano(texto: str) -> int:
    ano = int(texto)
    if len(texto) == 2:
        ano += 2000 if ano < 80 else 1900
    return ano


def orden_fechas(valores: Iterable[str]) -> str:
    """'dmy' (Spanish default) or 'mdy', decided for a whole column: a first part over 12 means
    day first; a second part over 12 in any row means month first."""
    mes_primero = dia_primero = False
    for bruto in valores:
        m = _FECHA_DMY.match(bruto.strip())
        if not m:
            continue
        if int(m.group(1)) > 12:
            dia_primero = True
        if int(m.group(2)) > 12:
            mes_primero = True
    if mes_primero and not dia_primero:
        return "mdy"
    if mes_primero and dia_primero:
        raise ErrorImportacion("Las fechas de la columna mezclan los formatos día/mes y mes/día.")
    return "dmy"


def leer_fecha(valor: object, orden: str = "dmy") -> date | None:
    """A cell -> date, or None when it is not a date (a footer, a blank row)."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, Decimal | int | float):
        serie = int(valor)
        if 20000 < serie < 80000:  # Excel serial day (1954-2119)
            return EPOCA_EXCEL + timedelta(days=serie)
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        if m := _FECHA_YMD.match(texto):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _FECHA_DMY.match(texto):
            a, b, ano = int(m.group(1)), int(m.group(2)), _ano(m.group(3))
            return date(ano, b, a) if orden == "dmy" else date(ano, a, b)
    except ValueError:
        return None
    return None


def enmascarar_cuenta(cuenta: str | None) -> str | None:
    """Keep only the last four digits of an account or IBAN."""
    if not cuenta:
        return None
    digitos = re.sub(r"\W", "", cuenta)
    return f"····{digitos[-4:]}" if len(digitos) >= 4 else None
