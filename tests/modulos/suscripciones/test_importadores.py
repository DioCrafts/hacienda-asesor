"""Statement importers: every fixture of the synthetic year reads back exactly, plus edge cases."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from hacienda_gpt.modulos.suscripciones.importadores import bytes_de_entrada, detectar_formato, importar
from hacienda_gpt.modulos.suscripciones.lectura import (
    decodificar,
    enmascarar_cuenta,
    leer_fecha,
    leer_importe,
    orden_fechas,
    separador_decimal,
)
from hacienda_gpt.modulos.suscripciones.modelos import ImportacionError
from tests.modulos.suscripciones.sintetico import FIXTURES, movimientos, norma43

FORMATOS = {
    "anual.n43": "norma43",
    "santander.csv": "csv",
    "bbva.csv": "csv",
    "ing.csv": "csv",
    "cargo_abono.tsv": "csv",
    "openbank.xlsx": "xlsx",
    "anual.ofx": "ofx",
    "anual.qfx": "qfx",
    "anual_camt053.xml": "camt053",
}


def _leer(nombre: str):
    return importar((FIXTURES / nombre).read_bytes())


@pytest.mark.parametrize("nombre", sorted(FORMATOS))
def test_cada_formato_reproduce_el_ano_sintetico(nombre: str) -> None:
    extracto = _leer(nombre)
    assert extracto.formato == FORMATOS[nombre]
    esperado = Counter((m.fecha, m.importe) for m in movimientos())
    leido = Counter((m.fecha, m.importe.quantize(Decimal("0.01"))) for m in extracto.movimientos)
    assert leido == esperado
    assert extracto.desde == date(2025, 10, 1) and extracto.hasta == date(2026, 10, 31)
    textos = " ".join(m.descripcion + " " + (m.contraparte or "") for m in extracto.movimientos)
    for marca in ("NETFLIX", "BASIC FIT", "IBERDROLA", "MAPFRE", "PAYPAL *SPOTIFY"):
        assert marca in textos


def test_deteccion_automatica_del_formato() -> None:
    for nombre, formato in FORMATOS.items():
        assert detectar_formato((FIXTURES / nombre).read_bytes()) == formato


def test_norma43_medios_cuenta_enmascarada_y_totales() -> None:
    extracto = _leer("anual.n43")
    assert extracto.cuenta == "····1332"
    medios = Counter(m.medio for m in extracto.movimientos)
    assert medios["adeudo"] > 0 and medios["tarjeta"] > 0 and medios["prestamo"] == 13
    netflix = next(m for m in extracto.movimientos if "NETFLIX" in m.descripcion)
    assert netflix.importe == Decimal("-12.99") and netflix.medio == "tarjeta"


def test_norma43_rechaza_totales_que_no_cuadran() -> None:
    texto = norma43(movimientos())
    lineas = texto.split("\r\n")
    indice = next(i for i, linea in enumerate(lineas) if linea.startswith("22"))
    linea = lineas[indice]
    lineas[indice] = linea[:28] + "00000000099999" + linea[42:]  # change the first amount
    with pytest.raises(ImportacionError, match="totales de control"):
        importar("\r\n".join(lineas).encode("latin-1"), "norma43")


def test_norma43_sin_saltos_de_linea_y_sin_registro_33() -> None:
    texto = norma43(movimientos()[:5])
    pegado = texto.replace("\r\n", "")
    assert len(importar(pegado.encode("latin-1"), "norma43").movimientos) == 5
    sin_final = "\r\n".join(linea for linea in texto.split("\r\n") if not linea.startswith(("33", "88")))
    with pytest.raises(ImportacionError, match="registro final"):
        importar(sin_final.encode("latin-1"), "norma43")


def test_camt_lote_pendiente_y_mandatos() -> None:
    extracto = _leer("anual_camt053.xml")
    assert any("BOOK" in a for a in extracto.avisos)
    gimnasio = [m for m in extracto.movimientos if m.contraparte == "BASIC FIT SPAIN SAU"]
    assert len(gimnasio) == 13 and all(m.referencia == "BF-000123" and m.medio == "adeudo" for m in gimnasio)
    assert extracto.cuenta == "····1332"


def test_camt_rechaza_doctype() -> None:
    malicioso = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Document><BkToCstmrStmt/></Document>'
    with pytest.raises(ImportacionError, match="DOCTYPE"):
        importar(malicioso, "camt053")


def test_ofx_codificacion_declarada_y_sin_signo() -> None:
    extracto = _leer("anual.ofx")
    assert extracto.cuenta == "····1332"
    sin_signo = (
        "OFXHEADER:100\nDATA:OFXSGML\nCHARSET:1252\n\n<OFX><BANKTRANLIST>"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260105<TRNAMT>12,99<NAME>NETFLIX.COM</STMTTRN>"
        "<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260106<TRNAMT>100.00<NAME>NÓMINA</STMTTRN></BANKTRANLIST></OFX>"
    ).encode("cp1252")
    leido = importar(sin_signo)
    assert [m.importe for m in leido.movimientos] == [Decimal("-12.99"), Decimal("100.00")]
    assert leido.movimientos[1].descripcion == "NÓMINA"
    assert any("signo" in a for a in leido.avisos)


def test_csv_columnas_ambiguas_piden_mapeo() -> None:
    csv = b"Fecha;Concepto;Importe;Cantidad\n01/02/2026;NETFLIX;-12,99;-12,99\n"
    with pytest.raises(ImportacionError, match="ambiguas"):
        importar(csv, "csv")
    extracto = importar(csv, "csv", mapeo_columnas={"importe": "Importe"})
    assert extracto.movimientos[0].importe == Decimal("-12.99")
    assert extracto.columnas["importe"] == "Importe"


def test_csv_importes_sin_signo_no_se_adivinan() -> None:
    csv = b"Fecha;Concepto;Importe\n01/02/2026;NETFLIX;12,99\n01/03/2026;NETFLIX;12,99\n"
    with pytest.raises(ImportacionError, match="importes_positivos_son"):
        importar(csv)
    extracto = importar(csv, importes_positivos_son="cargos")
    assert all(m.importe == Decimal("-12.99") for m in extracto.movimientos)
    assert any("como has indicado" in a for a in extracto.avisos)


def test_csv_sin_cabecera_reconocible() -> None:
    with pytest.raises(ImportacionError, match="cabecera"):
        importar(b"a;b;c\n1;2;3\n", "csv")


def test_csv_perfil_de_banco_no_coincidente_avisa() -> None:
    extracto = importar((FIXTURES / "ing.csv").read_bytes(), banco="santander")
    assert any("no coincide" in a for a in extracto.avisos)
    sin_aviso = importar((FIXTURES / "santander.csv").read_bytes(), banco="santander")
    assert sin_aviso.banco == "Banco Santander" and not sin_aviso.avisos


def test_csv_detecta_el_banco_por_el_preambulo() -> None:
    assert _leer("santander.csv").banco == "Banco Santander"
    assert _leer("openbank.xlsx").banco == "Openbank"


def test_formatos_no_soportados() -> None:
    with pytest.raises(ImportacionError, match="xls"):
        importar(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    with pytest.raises(ImportacionError, match="PDF"):
        importar(b"%PDF-1.7 ...")
    with pytest.raises(ImportacionError, match="Formato desconocido"):
        importar(b"x", "mt940")
    with pytest.raises(ImportacionError, match="contenido"):
        bytes_de_entrada(None, None)


@pytest.mark.parametrize(
    ("texto", "decimal", "esperado"),
    [
        ("-1.234,56 €", ",", "-1234.56"),
        ("1.234,56", ",", "1234.56"),
        ("12,99-", ",", "-12.99"),
        ("(12,99)", ",", "-12.99"),
        ("−12,99", ",", "-12.99"),
        ("+850,00 EUR", ",", "850.00"),
        ("-1,234.56", ".", "-1234.56"),
        ("12.99", ".", "12.99"),
    ],
)
def test_leer_importe(texto: str, decimal: str, esperado: str) -> None:
    assert leer_importe(texto, decimal) == Decimal(esperado)


def test_leer_importe_ilegible() -> None:
    with pytest.raises(ImportacionError):
        leer_importe("doce euros")


def test_separador_decimal_por_columna() -> None:
    assert separador_decimal(["-12,99", "1.234,00"]) == ","
    assert separador_decimal(["-12.99", "850.00"]) == "."
    assert separador_decimal(["1,234.56"]) == "."
    assert separador_decimal(["850"]) == ","


def test_fechas() -> None:
    assert leer_fecha("05/04/2026") == date(2026, 4, 5)
    assert leer_fecha("05-04-26") == date(2026, 4, 5)
    assert leer_fecha("2026-04-05") == date(2026, 4, 5)
    assert leer_fecha("04/25/2026", "mdy") == date(2026, 4, 25)
    assert leer_fecha(Decimal("46117")) == date(2026, 4, 5)
    assert leer_fecha("Saldo final") is None
    assert orden_fechas(["04/25/2026", "05/01/2026"]) == "mdy"
    assert orden_fechas(["25/04/2026"]) == "dmy"
    with pytest.raises(ImportacionError):
        orden_fechas(["25/04/2026", "04/25/2026"])


def test_decodificacion_y_mascara() -> None:
    assert decodificar("Fecha Operación".encode("cp1252")) == "Fecha Operación"
    assert decodificar("﻿Concepto".encode()) == "Concepto"
    assert enmascarar_cuenta("ES91 2100 0418 4502 0005 1332") == "····1332"
    assert enmascarar_cuenta(None) is None


def test_fixture_dir_no_contiene_nada_mas() -> None:
    assert {p.name for p in Path(FIXTURES).iterdir()} == set(FORMATOS)
