"""Reader of tabular bank exports: CSV (any delimiter, any of the usual encodings) and XLSX.

Columns are recognised ONLY by the name of their header (``data/bancos.json``), never by position, and the
mapping actually used is returned so the tool can show it. When a field matches more than one column, or
the amounts carry no sign, the reader stops and asks (``mapeo_columnas`` / ``importes_positivos_son``)
instead of guessing. Old binary ``.xls`` files are not supported.

The XLSX reader is a minimal OOXML reader (zip + shared strings + first worksheet + date styles) with no
third-party dependency, with limits against zip bombs.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import io
import json
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from hacienda_gpt.modulos.suscripciones.lectura import (
    EPOCA_EXCEL,
    decodificar,
    leer_fecha,
    leer_importe,
    orden_fechas,
    separador_decimal,
    sin_acentos,
)
from hacienda_gpt.modulos.suscripciones.modelos import ErrorImportacion, Extracto, Movimiento

DATA = Path(__file__).parent / "data" / "bancos.json"
Celda = str | Decimal | date | None
_FILAS_CABECERA = 40
_CRITICOS = {"fecha", "fecha_valor", "importe", "cargo", "abono", "signo"}


@lru_cache(maxsize=1)
def configuracion_bancos() -> dict[str, Any]:
    datos: dict[str, Any] = json.loads(DATA.read_text(encoding="utf-8"))
    return datos


def normalizar_cabecera(texto: object) -> str:
    """'F. Operación' -> 'f operacion'; 'IMPORTE (€)' -> 'importe'."""
    limpio = sin_acentos(str(texto or "")).lower()
    limpio = re.sub(r"\((?:€|eur|euros)\)|€", " ", limpio)
    limpio = re.sub(r"[^a-z0-9]+", " ", limpio)
    return " ".join(limpio.split())


def _alias() -> dict[str, set[str]]:
    return {campo: {normalizar_cabecera(a) for a in alias} for campo, alias in configuracion_bancos()["campos"].items()}


# --- CSV and XLSX -> rows ------------------------------------------------------------------------


def filas_csv(datos: bytes | str) -> list[list[Celda]]:
    texto = datos if isinstance(datos, str) else decodificar(datos)
    texto = texto.lstrip("﻿")
    muestra = "\n".join(texto.splitlines()[:50])
    # The delimiter is the candidate that splits most lines into the same number (> 1) of fields.
    mejor, puntos = ";", -1
    for delimitador in (";", ",", "\t", "|"):
        cuentas = [len(fila) for fila in csv.reader(io.StringIO(muestra), delimiter=delimitador) if any(fila)]
        if not cuentas:
            continue
        moda = max(set(cuentas), key=cuentas.count)
        valor = cuentas.count(moda) * (moda > 1) * moda
        if valor > puntos:
            mejor, puntos = delimitador, valor
    return [list(fila) for fila in csv.reader(io.StringIO(texto), delimiter=mejor)]


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_MAX_XML = 50 * 1024 * 1024
# Built-in number formats that are dates (ECMA-376 part 1, 18.8.30).
_FORMATOS_FECHA = set(range(14, 23)) | {45, 46, 47}


def _leer_zip(zona: zipfile.ZipFile, nombre: str) -> bytes:
    info = zona.getinfo(nombre)
    if info.file_size > _MAX_XML:
        raise ErrorImportacion("El XLSX es demasiado grande una vez descomprimido.")
    datos = zona.read(info)
    if re.search(rb"<!DOCTYPE|<!ENTITY", datos[:4096]):
        raise ErrorImportacion("El XLSX contiene declaraciones DOCTYPE; por seguridad no se procesa.")
    return datos


def _columna(referencia: str) -> int:
    letras = re.match(r"[A-Z]+", referencia)
    numero = 0
    for letra in letras.group(0) if letras else "A":
        numero = numero * 26 + ord(letra) - 64
    return numero - 1


def filas_xlsx(datos: bytes) -> list[list[Celda]]:
    try:
        zona = zipfile.ZipFile(io.BytesIO(datos))
    except zipfile.BadZipFile as exc:
        raise ErrorImportacion("El fichero XLSX está dañado (no es un ZIP válido).") from exc
    with zona:
        nombres = set(zona.namelist())
        libro = ET.fromstring(_leer_zip(zona, "xl/workbook.xml"))
        hoja = libro.find("m:sheets/m:sheet", _NS)
        if hoja is None:
            raise ErrorImportacion("El XLSX no tiene hojas.")
        destino = "xl/worksheets/sheet1.xml"
        if "xl/_rels/workbook.xml.rels" in nombres:
            relaciones = ET.fromstring(_leer_zip(zona, "xl/_rels/workbook.xml.rels"))
            for rel in relaciones:
                if rel.get("Id") == hoja.get(_REL):
                    objetivo = rel.get("Target", "")
                    destino = objetivo.lstrip("/") if objetivo.startswith("/") else f"xl/{objetivo}"
        compartidas: list[str] = []
        if "xl/sharedStrings.xml" in nombres:
            for si in ET.fromstring(_leer_zip(zona, "xl/sharedStrings.xml")).findall("m:si", _NS):
                compartidas.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        estilos_fecha: set[int] = set()
        if "xl/styles.xml" in nombres:
            estilos = ET.fromstring(_leer_zip(zona, "xl/styles.xml"))
            propios = {
                int(nf.get("numFmtId", "0")): nf.get("formatCode", "")
                for nf in estilos.findall("m:numFmts/m:numFmt", _NS)
            }
            for indice, xf in enumerate(estilos.findall("m:cellXfs/m:xf", _NS)):
                formato = int(xf.get("numFmtId", "0"))
                codigo = re.sub(r'"[^"]*"|\[[^\]]*\]', "", propios.get(formato, "")).lower()
                if formato in _FORMATOS_FECHA or (formato in propios and re.search(r"[dy]", codigo)):
                    estilos_fecha.add(indice)
        if destino not in nombres:
            raise ErrorImportacion("El XLSX no contiene la hoja indicada en el libro.")
        filas: list[list[Celda]] = []
        for fila in ET.fromstring(_leer_zip(zona, destino)).iter(f"{{{_NS['m']}}}row"):
            valores: list[Celda] = []
            for celda in fila.findall("m:c", _NS):
                posicion = _columna(celda.get("r", "")) if celda.get("r") else len(valores)
                valores.extend([None] * (posicion - len(valores)))
                valores.append(_valor_celda(celda, compartidas, estilos_fecha))
            filas.append(valores)
        return filas


def _valor_celda(celda: ET.Element, compartidas: list[str], estilos_fecha: set[int]) -> Celda:
    tipo = celda.get("t", "n")
    if tipo == "inlineStr":
        return "".join(t.text or "" for t in celda.iter(f"{{{_NS['m']}}}t"))
    valor = celda.findtext("m:v", default=None, namespaces=_NS)
    if valor is None:
        return None
    if tipo == "s":
        return compartidas[int(valor)]
    if tipo in {"str", "b", "e"}:
        return valor
    try:
        numero = Decimal(valor)
    except InvalidOperation:
        return valor
    if int(celda.get("s", "0")) in estilos_fecha:
        return EPOCA_EXCEL + timedelta(days=int(numero))
    return numero


# --- Rows -> movements ---------------------------------------------------------------------------


def _detectar_banco(filas: list[list[Celda]], cabecera: int) -> str | None:
    preambulo = " " + " ".join(normalizar_cabecera(c) for fila in filas[:cabecera] for c in fila if c) + " "
    for clave, banco in configuracion_bancos()["bancos"].items():
        if any(f" {normalizar_cabecera(i)} " in preambulo for i in banco["identificadores"]):
            return str(clave)
    return None


def _mapear(
    cabecera: list[Celda], mapeo: dict[str, str] | None
) -> tuple[dict[str, int], list[int], dict[str, list[int]]]:
    """field -> column index, detail columns, and fields matched by several columns (ambiguous)."""
    alias = _alias()
    normal = [normalizar_cabecera(c) for c in cabecera]
    encontrados: dict[str, list[int]] = {}
    for indice, nombre in enumerate(normal):
        for campo, nombres in alias.items():
            if nombre and nombre in nombres:
                encontrados.setdefault(campo, []).append(indice)
    for campo, cabecera_usuario in (mapeo or {}).items():
        if campo not in alias:
            raise ErrorImportacion(f"Campo desconocido en mapeo_columnas: «{campo}».")
        buscada = normalizar_cabecera(cabecera_usuario)
        if buscada not in normal:
            raise ErrorImportacion(f"La columna «{cabecera_usuario}» indicada para {campo} no está en la cabecera.")
        for otros in encontrados.values():
            if normal.index(buscada) in otros:
                otros.remove(normal.index(buscada))
        encontrados[campo] = [normal.index(buscada)]
    detalle = list(encontrados.pop("detalle", []))
    conceptos = encontrados.get("concepto", [])
    if len(conceptos) > 1:  # several description columns: the first is the concept, the rest detail
        detalle = conceptos[1:] + detalle
        encontrados["concepto"] = conceptos[:1]
    # Only the fields that decide dates and amounts must be unique; balance or currency are never used.
    ambiguos = {campo: cols for campo, cols in encontrados.items() if len(cols) > 1 and campo in _CRITICOS}
    columnas = {campo: cols[0] for campo, cols in encontrados.items() if cols}
    return columnas, detalle, ambiguos


def _puntuacion(fila: list[Celda]) -> int:
    alias = _alias()
    campos = {campo for c in fila for campo, nombres in alias.items() if normalizar_cabecera(c) in nombres}
    if not ({"fecha", "fecha_valor"} & campos and {"importe", "cargo", "abono"} & campos and "concepto" in campos):
        return 0
    return len(campos)


def tabla_a_extracto(
    filas: list[list[Celda]],
    formato: str,
    banco: str | None = None,
    mapeo_columnas: dict[str, str] | None = None,
    importes_positivos_son: str | None = None,
) -> Extracto:
    candidatas = [(i, _puntuacion(fila)) for i, fila in enumerate(filas[:_FILAS_CABECERA])]
    if mapeo_columnas:
        buscadas = {normalizar_cabecera(v) for v in mapeo_columnas.values()}
        candidatas = [
            (i, 100) for i, fila in enumerate(filas[:_FILAS_CABECERA]) if buscadas <= {normalizar_cabecera(c) for c in fila}
        ] or candidatas
    indice, puntos = max(candidatas, key=lambda par: (par[1], -par[0]), default=(0, 0))
    if puntos == 0:
        raise ErrorImportacion(
            "No se ha encontrado la fila de cabecera (hacen falta columnas de fecha, concepto e importe o "
            "cargo/abono). Indica los nombres de las columnas en mapeo_columnas, p. ej. "
            '{"fecha": "F. Operación", "concepto": "Concepto", "importe": "Importe"}.'
        )
    cabecera = filas[indice]
    columnas, detalle, ambiguos = _mapear(cabecera, mapeo_columnas)
    if ambiguos:
        lista = "; ".join(f"{campo}: " + ", ".join(f"«{cabecera[c]}»" for c in cols) for campo, cols in ambiguos.items())
        raise ErrorImportacion(
            f"Hay columnas ambiguas ({lista}). Indica cuál usar con mapeo_columnas, p. ej. "
            '{"importe": "Importe"}.'
        )
    if "fecha" not in columnas:
        if "fecha_valor" not in columnas:
            raise ErrorImportacion("No se ha encontrado la columna de fecha; indícala en mapeo_columnas.")
        columnas["fecha"] = columnas.pop("fecha_valor")
    if "concepto" not in columnas or not {"importe", "cargo", "abono"} & set(columnas):
        raise ErrorImportacion("Faltan las columnas de concepto o de importe; indícalas en mapeo_columnas.")
    avisos: list[str] = []
    datos = [fila for fila in filas[indice + 1 :] if any(c not in (None, "") for c in fila)]

    def celda(fila: list[Celda], campo: str) -> Celda:
        col = columnas.get(campo)
        return fila[col] if col is not None and col < len(fila) else None

    orden = orden_fechas(str(celda(f, "fecha")) for f in datos if isinstance(celda(f, "fecha"), str))
    decimales = {
        campo: separador_decimal(str(celda(f, campo)) for f in datos if isinstance(celda(f, campo), str))
        for campo in ("importe", "cargo", "abono")
    }

    def importe_de(fila: list[Celda], campo: str) -> Decimal | None:
        valor = celda(fila, campo)
        if valor is None or valor == "":
            return None
        if isinstance(valor, Decimal):
            return valor
        if isinstance(valor, date):
            raise ErrorImportacion(f"La columna de {campo} contiene fechas: revisa el mapeo de columnas.")
        return leer_importe(valor, decimales[campo])

    crudos: list[tuple[date, Decimal, str, str | None, date | None]] = []
    omitidas = 0
    for fila in datos:
        fecha = leer_fecha(celda(fila, "fecha"), orden)
        if fecha is None:
            omitidas += 1
            continue
        if "importe" in columnas:
            importe = importe_de(fila, "importe")
            if importe is None:
                omitidas += 1
                continue
            signo = normalizar_cabecera(celda(fila, "signo")) if "signo" in columnas else ""
            if signo in {"d", "debe", "cargo", "debito"}:
                importe = -abs(importe)
            elif signo in {"h", "haber", "abono", "credito"}:
                importe = abs(importe)
        else:
            cargo, abono = importe_de(fila, "cargo"), importe_de(fila, "abono")
            if cargo is None and abono is None:
                omitidas += 1
                continue
            importe = (abs(abono) if abono else Decimal(0)) - (abs(cargo) if cargo else Decimal(0))
        concepto = str(celda(fila, "concepto") or "").strip()
        extra = " ".join(str(fila[c]).strip() for c in detalle if c < len(fila) and fila[c] not in (None, ""))
        crudos.append(
            (fecha, importe, concepto or extra or "Movimiento", extra or None, leer_fecha(celda(fila, "fecha_valor"), orden))
        )
    if not crudos:
        raise ErrorImportacion("La tabla no contiene movimientos con fecha e importe legibles.")
    if omitidas:
        avisos.append(f"Se han ignorado {omitidas} filas sin fecha o importe legibles (totales, pies de página…).")
    if "importe" in columnas and "signo" not in columnas and all(imp >= 0 for _, imp, *_ in crudos):
        if importes_positivos_son not in {"cargos", "abonos"}:
            raise ErrorImportacion(
                "Todos los importes son positivos y no hay columna de signo: no se puede distinguir cargos de abonos. "
                "Indica importes_positivos_son = 'cargos' o 'abonos'."
            )
        if importes_positivos_son == "cargos":
            crudos = [(f, -imp, c, d, v) for f, imp, c, d, v in crudos]
        avisos.append(f"Importes sin signo: tratados como {importes_positivos_son}, como has indicado.")
    banco_detectado = banco or _detectar_banco(filas, indice)
    perfiles = configuracion_bancos()["bancos"]
    if banco and banco in perfiles:
        esperadas = {normalizar_cabecera(c) for c in perfiles[banco]["cabeceras_esperadas"]}
        presentes = {normalizar_cabecera(c) for c in cabecera}
        if not esperadas <= presentes:
            avisos.append(
                f"La cabecera no coincide con la esperada para {perfiles[banco]['nombre']} (perfil no verificado); "
                "se han reconocido las columnas por su nombre."
            )
    movimientos = tuple(
        sorted(
            (
                Movimiento(fecha=f, importe=imp, descripcion=c, contraparte=d if d != c else None, fecha_valor=v)
                for f, imp, c, d, v in crudos
            ),
            key=lambda m: m.fecha,
        )
    )
    usadas = {campo: str(cabecera[col]) for campo, col in columnas.items()}
    if detalle:
        usadas["detalle"] = ", ".join(str(cabecera[c]) for c in detalle)
    nombre_banco = perfiles.get(banco_detectado, {}).get("nombre") if banco_detectado else None
    return Extracto(
        formato=formato,
        movimientos=movimientos,
        avisos=tuple(avisos),
        banco=nombre_banco or banco,
        columnas=usadas,
    )
