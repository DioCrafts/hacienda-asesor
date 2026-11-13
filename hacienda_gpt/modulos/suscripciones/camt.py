"""Reader of ISO 20022 cash-management statements: camt.053 (BankToCustomerStatement) and, with the same
entry structure, camt.052 (account report) and camt.054 (debit/credit notification).

Namespace-agnostic (any ``urn:iso:std:iso:20022:tech:xsd:camt.05x.001.NN`` version): elements are found by
local name. Per entry (``Ntry``): ``Amt`` with ``Ccy``, ``CdtDbtInd`` (CRDT/DBIT), ``Sts`` (BOOK/PDNG/INFO;
plain text up to version 07, ``Sts/Cd`` from 08), ``BookgDt`` and ``ValDt`` (``Dt`` or ``DtTm``),
``BkTxCd`` (ISO bank transaction code: domain / family / sub-family), ``AddtlNtryInf`` and the
transaction details (``NtryDtls/TxDtls``): mandate id (``Refs/MndtId``), related parties (``Cdtr``/``Dbtr``
with ``Nm`` or, from version 08, ``Pty/Nm``) and unstructured remittance (``RmtInf/Ustrd``). A batch entry
with several ``TxDtls`` carrying their own amounts becomes one movement per transaction. Pending and
information-only entries are skipped (and said so).

Untrusted XML: documents with a DOCTYPE or entity declarations are rejected before parsing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import xml.etree.ElementTree as ET

from hacienda_gpt.modulos.suscripciones.lectura import enmascarar_cuenta
from hacienda_gpt.modulos.suscripciones.modelos import Extracto, ImportacionError, Medio, Movimiento

_Elemento = ET.Element


def es_camt(texto: str) -> bool:
    cabeza = texto[:5000]
    return any(tag in cabeza for tag in ("BkToCstmrStmt", "BkToCstmrAcctRpt", "BkToCstmrDbtCdtNtfctn"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _hijo(el: _Elemento | None, *ruta: str) -> _Elemento | None:
    for nombre in ruta:
        if el is None:
            return None
        el = next((h for h in el if _local(h.tag) == nombre), None)
    return el


def _hijos(el: _Elemento | None, nombre: str) -> list[_Elemento]:
    return [] if el is None else [h for h in el if _local(h.tag) == nombre]


def _texto(el: _Elemento | None, *ruta: str) -> str | None:
    destino = _hijo(el, *ruta) if ruta else el
    if destino is None or destino.text is None:
        return None
    return " ".join(destino.text.split()) or None


def _fecha(el: _Elemento | None) -> date | None:
    valor = _texto(el, "Dt") or _texto(el, "DtTm")
    if not valor:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError as exc:
        raise ImportacionError(f"camt: fecha ilegible «{valor}».") from exc


def _importe(el: _Elemento | None) -> tuple[Decimal, str] | None:
    if el is None or not el.text:
        return None
    try:
        return Decimal(el.text.strip()), el.get("Ccy", "EUR")
    except InvalidOperation as exc:
        raise ImportacionError(f"camt: importe ilegible «{el.text.strip()}».") from exc


def _medio(entrada: _Elemento, mandato: str | None) -> Medio:
    codigo = _hijo(entrada, "BkTxCd", "Domn")
    dominio = _texto(codigo, "Cd") or ""
    familia = _texto(codigo, "Fmly", "Cd") or ""
    subfamilia = _texto(codigo, "Fmly", "SubFmlyCd") or ""
    if subfamilia in {"CHRG", "FEES", "COMM"}:
        return "comision"
    if dominio == "LDAS":
        return "prestamo"
    if familia in {"CCRD", "MCRD"}:
        return "efectivo" if subfamilia == "CWDL" else "tarjeta"
    if familia == "RDDT" or mandato:
        return "adeudo"
    if familia in {"ICDT", "RCDT"}:
        return "transferencia"
    return "desconocido"


def _parte(tx: _Elemento | None, rol: str) -> str | None:
    partes = _hijo(tx, "RltdPties")
    return _texto(partes, rol, "Nm") or _texto(partes, rol, "Pty", "Nm")


def leer_camt(texto: str) -> Extracto:
    if re.search(r"<!DOCTYPE|<!ENTITY", texto[:10000], re.I):
        raise ImportacionError("El XML declara un DOCTYPE o entidades; por seguridad no se procesa.")
    try:
        raiz = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", texto.lstrip("﻿")))
    except ET.ParseError as exc:
        raise ImportacionError(f"camt: el XML no está bien formado ({exc}).") from exc
    mensaje = next(iter(raiz), None)
    if mensaje is None or _local(mensaje.tag) not in {"BkToCstmrStmt", "BkToCstmrAcctRpt", "BkToCstmrDbtCdtNtfctn"}:
        raise ImportacionError("El XML no es un extracto camt.053/052/054 (falta BkToCstmrStmt).")
    informes = [h for h in mensaje if _local(h.tag) in {"Stmt", "Rpt", "Ntfctn"}]
    movimientos: list[Movimiento] = []
    avisos: list[str] = []
    pendientes = 0
    cuenta = None
    for informe in informes:
        cuenta = cuenta or _texto(informe, "Acct", "Id", "IBAN") or _texto(informe, "Acct", "Id", "Othr", "Id")
        for entrada in _hijos(informe, "Ntry"):
            estado = _texto(entrada, "Sts") or _texto(entrada, "Sts", "Cd") or "BOOK"
            if estado != "BOOK":
                pendientes += 1
                continue
            leido = _importe(_hijo(entrada, "Amt"))
            indicador = _texto(entrada, "CdtDbtInd")
            if leido is None or indicador not in {"CRDT", "DBIT"}:
                raise ImportacionError("camt: hay un apunte sin importe (Amt) o sin indicador CRDT/DBIT.")
            fecha = _fecha(_hijo(entrada, "BookgDt")) or _fecha(_hijo(entrada, "ValDt"))
            if fecha is None:
                raise ImportacionError("camt: hay un apunte sin fecha (BookgDt/ValDt).")
            fecha_valor = _fecha(_hijo(entrada, "ValDt"))
            signo = -1 if indicador == "DBIT" else 1
            rol = "Cdtr" if indicador == "DBIT" else "Dbtr"
            info = _texto(entrada, "AddtlNtryInf")
            transacciones = [tx for detalle in _hijos(entrada, "NtryDtls") for tx in _hijos(detalle, "TxDtls")]
            con_importe = [
                tx
                for tx in transacciones
                if _hijo(tx, "AmtDtls", "TxAmt", "Amt") is not None or _hijo(tx, "Amt") is not None
            ]
            partes: list[_Elemento | None] = (
                list(con_importe) if len(con_importe) > 1 else [transacciones[0] if transacciones else None]
            )
            for tx in partes:
                importe, divisa = leido
                if len(partes) > 1:
                    propio = _importe(_hijo(tx, "AmtDtls", "TxAmt", "Amt")) or _importe(_hijo(tx, "Amt"))
                    if propio:
                        importe, divisa = propio
                mandato = _texto(tx, "Refs", "MndtId") if tx is not None else None
                ustrd = " ".join(t for el in _hijos(_hijo(tx, "RmtInf"), "Ustrd") if (t := _texto(el))) or None
                contraparte = _parte(tx, rol) if tx is not None else None
                descripcion = ustrd or info or contraparte or "Apunte"
                movimientos.append(
                    Movimiento(
                        fecha=fecha,
                        fecha_valor=fecha_valor,
                        importe=signo * importe,
                        descripcion=descripcion,
                        contraparte=contraparte,
                        referencia=mandato,
                        medio=_medio(entrada, mandato),
                        divisa=divisa,
                    )
                )
    if not informes:
        raise ImportacionError("camt: el mensaje no contiene ningún extracto (Stmt).")
    if pendientes:
        avisos.append(f"Se han omitido {pendientes} apuntes no contabilizados (estado distinto de BOOK).")
    return Extracto(
        formato="camt053",
        movimientos=tuple(sorted(movimientos, key=lambda m: m.fecha)),
        avisos=tuple(avisos),
        cuenta=enmascarar_cuenta(cuenta),
    )
