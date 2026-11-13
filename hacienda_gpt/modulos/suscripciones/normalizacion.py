"""Deterministic normalisation of merchant descriptors and lookup in the merchant catalogue.

A bank concept such as ``COMPRA TARJ. 5540XXXXXXXX1234 NETFLIX.COM 866-579-7172 NL 12/03`` becomes the
merchant key ``NETFLIX``: card numbers, dates, references, phone numbers, payment-method words, legal
suffixes and country/city tails are removed. Known merchants (``data/comercios.json``) get a label, a
category and the legal regime of their cancellation; unknown ones keep the cleaned key.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

from hacienda_gpt.modulos.suscripciones.lectura import sin_acentos
from hacienda_gpt.modulos.suscripciones.modelos import Movimiento

DATA = Path(__file__).parent / "data" / "comercios.json"

# Words that describe the operation, not the merchant.
_GENERICAS = {
    "A", "ACREEDOR", "ADEUDO", "ADEUDOS", "APPLE PAY", "CARGO", "CARGOS", "COBRO", "COMERCIO", "COMPRA", "COMPRAS",
    "CON", "CONCEPTO", "CONTACTLESS", "CORE", "DD", "DE", "DEL", "DOMICILIACION", "DOMICILIADO", "DOMICILIADA",
    "EMITIDA", "EN", "FACTURA", "FAVOR", "FRA", "ID", "INTERNET", "LA", "EL", "LOS", "MANDATO", "MOVIL", "NO", "NUM",
    "OPERACION", "ORDEN", "PAGO", "PAGOS", "PERIODICA", "POR", "PRESENTADO", "RECIBO", "RECIBOS", "REF",
    "REFERENCIA", "SEPA", "SU", "TARJ", "TARJETA", "TRANSACCION", "TRANSF", "TRANSFERENCIA", "Y", "WWW", "COM",
    "ES", "ESP", "ESPANA", "SPAIN", "NL", "IE", "LU", "GB", "US", "FR", "PT", "IT", "MADRID", "BARCELONA",
    "VALENCIA", "SEVILLA", "DUBLIN", "CORK", "LUXEMBOURG", "LUXEMBURGO", "AMSTERDAM", "LONDON", "LONDRES",
    "STOCKHOLM", "PARIS", "SA", "SL", "SAU", "SLU", "BV", "LTD", "LIMITED", "INC", "LLC", "GMBH", "SARL", "AB",
    "S", "L", "U", "INTERNATIONAL", "EUROPE", "EU", "ET", "CIE", "SCA", "EUR", "EUROS",
}
_GENERICAS_FRASES = ("APPLE PAY", "GOOGLE PAY", "SAMSUNG PAY")
_TARJETA = re.compile(r"\b[\dX*]*[\d*][\dX*]{3,}\b|\*+\d+")
_FECHA = re.compile(r"\b\d{1,4}[/\-.]\d{1,2}(?:[/\-.]\d{1,4})?\b")


@dataclass(frozen=True)
class Comercio:
    patron: re.Pattern[str]
    etiqueta: str
    categoria: str
    sector: str
    recurrente: bool


@dataclass(frozen=True)
class Identidad:
    clave: str  # merchant key that groups charges
    etiqueta: str  # label for the user
    categoria: str
    sector: str
    recurrente: bool  # False: a merchant whose charges are never a subscription (supermarket, ATM…)
    en_catalogo: bool
    canal: str  # "paypal" or the payment means; separates the same service paid twice


@lru_cache(maxsize=1)
def catalogo() -> tuple[Comercio, ...]:
    datos = json.loads(DATA.read_text(encoding="utf-8"))
    return tuple(
        Comercio(re.compile(c["patron"]), c["etiqueta"], c["categoria"], c["sector"], bool(c["recurrente"]))
        for c in datos["comercios"]
    )


def texto_ligero(texto: str) -> str:
    """Upper case, no accents, no card numbers or dates, punctuation as spaces ('+' becomes PLUS)."""
    limpio = sin_acentos(texto or "").upper()
    limpio = _TARJETA.sub(" ", limpio)
    limpio = _FECHA.sub(" ", limpio)
    limpio = limpio.replace("+", " PLUS ")
    limpio = re.sub(r"[^A-Z0-9 ]+", " ", limpio)
    return " ".join(limpio.split())


def clave_comercio(texto: str) -> str:
    """Merchant key: the first three meaningful words of the descriptor, '' when nothing is left."""
    limpio = texto_ligero(texto)
    for frase in _GENERICAS_FRASES:
        limpio = limpio.replace(frase, " ")
    palabras = [p for p in limpio.split() if not any(c.isdigit() for c in p) and p not in _GENERICAS]
    if len(palabras) > 1 and palabras[0] == "PAYPAL":
        palabras = palabras[1:]
    return " ".join(palabras[:3])


def buscar_en_catalogo(*textos: str | None) -> Comercio | None:
    ligero = " ".join(texto_ligero(t) for t in textos if t)
    return next((c for c in catalogo() if c.patron.search(ligero)), None)


def identificar(movimiento: Movimiento) -> Identidad:
    canal = "paypal" if "PAYPAL" in (movimiento.descripcion + " " + (movimiento.contraparte or "")).upper() else ""
    canal = canal or movimiento.medio
    comercio = buscar_en_catalogo(movimiento.descripcion, movimiento.contraparte)
    if comercio is not None:
        return Identidad(
            clave=clave_comercio(comercio.etiqueta) or comercio.etiqueta.upper(),
            etiqueta=comercio.etiqueta,
            categoria=comercio.categoria,
            sector=comercio.sector,
            recurrente=comercio.recurrente,
            en_catalogo=True,
            canal=canal,
        )
    # A direct debit names its creditor best; otherwise the concept, then the counterparty.
    candidatos = [movimiento.descripcion, movimiento.contraparte]
    if movimiento.medio == "adeudo" and movimiento.contraparte:
        candidatos.reverse()
    clave = next((k for t in candidatos if t and (k := clave_comercio(t))), "") or "SIN CONCEPTO"
    categoria = {"prestamo": "prestamos", "comision": "banca_comisiones"}.get(movimiento.medio, "otros")
    sector = {"prestamos": "financiero", "banca_comisiones": "financiero"}.get(categoria, "general")
    if movimiento.medio in {"transferencia", "efectivo"}:
        sector = "no_aplica"
    return Identidad(
        clave=clave,
        etiqueta=clave.title(),
        categoria=categoria,
        sector=sector,
        recurrente=movimiento.medio != "efectivo",
        en_catalogo=False,
        canal=canal,
    )
