"""Reader of OFX / QFX statements (Open Financial Exchange 1.x SGML and 2.x XML; QFX is OFX with Intuit
headers). Only the bank statement transaction list is read: ``<STMTTRN>`` aggregates with ``TRNTYPE``,
``DTPOSTED``, ``TRNAMT``, ``FITID``, ``NAME``/``PAYEE`` and ``MEMO``.

OFX 1.x does not close leaf elements, so the reader is a tolerant tag scanner instead of an XML parser:
it reads ``<TAG>value`` up to the next ``<`` and works for both versions. Per the specification the
amount is signed (negative = money out); a few banks write the decimal with a comma, which is accepted.
"""

from __future__ import annotations

from datetime import date
import html
import re

from hacienda_gpt.modulos.suscripciones.lectura import enmascarar_cuenta, leer_importe
from hacienda_gpt.modulos.suscripciones.modelos import Extracto, ImportacionError, Medio, Movimiento

_MEDIO_POR_TIPO: dict[str, Medio] = {
    "DIRECTDEBIT": "adeudo",
    "POS": "tarjeta",
    "XFER": "transferencia",
    "ATM": "efectivo",
    "FEE": "comision",
    "SRVCHG": "comision",
    "PAYMENT": "adeudo",
    "REPEATPMT": "transferencia",
}
# Types that are money out even if a bank writes the amount without its sign.
_TIPOS_CARGO = {"DEBIT", "DIRECTDEBIT", "POS", "ATM", "FEE", "SRVCHG", "PAYMENT", "CHECK"}

_BLOQUE = re.compile(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|$)", re.S | re.I)


def es_ofx(texto: str) -> bool:
    cabeza = texto[:4000].upper()
    return "OFXHEADER" in cabeza or "<OFX>" in cabeza


def codificacion_declarada(cabecera: bytes) -> str | None:
    """The encoding an OFX header declares (1.x ``CHARSET:1252`` or 2.x ``encoding="UTF-8"``)."""
    texto = cabecera[:2000].decode("ascii", errors="ignore").upper()
    if m := re.search(r'ENCODING="([A-Z0-9_\-]+)"', texto):
        return m.group(1).lower()
    if m := re.search(r"CHARSET:\s*(\d{3,4})", texto):
        return f"cp{m.group(1)}"
    if re.search(r"ENCODING:\s*UTF-?8", texto):
        return "utf-8"
    return None


def _etiqueta(bloque: str, nombre: str) -> str | None:
    m = re.search(rf"<{nombre}>([^<\r\n]*)", bloque, re.I)
    if not m:
        return None
    valor = html.unescape(m.group(1)).strip()
    return valor or None


def _fecha_ofx(texto: str) -> date:
    m = re.match(r"(\d{4})(\d{2})(\d{2})", texto)
    if not m:
        raise ImportacionError(f"OFX: fecha ilegible «{texto}».")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise ImportacionError(f"OFX: fecha ilegible «{texto}».") from exc


def leer_ofx(texto: str, formato: str = "ofx") -> Extracto:
    if not es_ofx(texto):
        raise ImportacionError("El fichero no parece OFX/QFX (no tiene cabecera OFXHEADER ni elemento <OFX>).")
    divisa = (_etiqueta(texto, "CURDEF") or "EUR").upper()
    cuenta = _etiqueta(texto, "ACCTID")
    crudos = []
    for bloque in _BLOQUE.findall(texto):
        tipo = (_etiqueta(bloque, "TRNTYPE") or "OTHER").upper()
        fecha = _etiqueta(bloque, "DTPOSTED") or _etiqueta(bloque, "DTUSER")
        importe = _etiqueta(bloque, "TRNAMT")
        if not fecha or not importe:
            raise ImportacionError("OFX: hay un movimiento sin fecha (DTPOSTED) o sin importe (TRNAMT).")
        decimal = "," if re.search(r",\d{1,2}$", importe) else "."
        nombre = _etiqueta(bloque, "NAME") or _etiqueta(bloque, "PAYEE")
        memo = _etiqueta(bloque, "MEMO")
        crudos.append(
            (tipo, _fecha_ofx(fecha), leer_importe(importe, decimal), nombre, memo, _etiqueta(bloque, "FITID"))
        )
    if not crudos:
        raise ImportacionError("OFX: el fichero no contiene movimientos (<STMTTRN>).")
    avisos: list[str] = []
    # A bank that writes every amount unsigned: fall back on the transaction type, and say so.
    sin_signo = all(importe >= 0 for _, _, importe, *_ in crudos) and any(t in _TIPOS_CARGO for t, *_ in crudos)
    if sin_signo:
        avisos.append("Los importes del OFX no llevan signo; los cargos se deducen del tipo de movimiento (TRNTYPE).")
    movimientos = []
    for tipo, fecha_mov, importe_mov, nombre, memo, fitid in crudos:
        if sin_signo and tipo in _TIPOS_CARGO:
            importe_mov = -importe_mov
        movimientos.append(
            Movimiento(
                fecha=fecha_mov,
                importe=importe_mov,
                descripcion=nombre or memo or tipo,
                contraparte=memo if nombre and memo and memo != nombre else None,
                referencia=fitid,
                medio=_MEDIO_POR_TIPO.get(tipo, "desconocido"),
                divisa=divisa,
            )
        )
    return Extracto(
        formato=formato,
        movimientos=tuple(sorted(movimientos, key=lambda m: m.fecha)),
        avisos=tuple(avisos),
        cuenta=enmascarar_cuenta(cuenta),
    )
