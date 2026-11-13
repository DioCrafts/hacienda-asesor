"""The reader of the Banco de España resolutions in the BOE, and the stored series against its sources.

The fixtures are real BOE XML files (``/diario_boe/xml.php?id=…``), one per format used since 2004,
plus the «Corrección de erratas» the series applies. No network.
"""

from __future__ import annotations

from datetime import date
import decimal
import hashlib
import re

from hipoteca_util import RESOLUCIONES
import pytest

from hacienda_gpt.modulos.hipoteca.indices import (
    BOE_XML_URL,
    MES_INFERIDO,
    FormatoResolucionError,
    actualizar,
    cargar_serie,
    construir_serie,
    ids_en_resultados,
    leer_resoluciones,
    parsear_resolucion,
    serie_desde_json,
)

D = decimal.Decimal


def leer(boe_id: str) -> bytes:
    return (RESOLUCIONES / f"{boe_id}.xml").read_bytes()


def valores(boe_id: str) -> dict[tuple[str, str], D]:
    return {(v.mes, v.indice): v.valor for v in parsear_resolucion(leer(boe_id)).valores}


def test_formato_actual_principio_de_mes():
    res = parsear_resolucion(leer("BOE-A-2026-23168"))
    assert res.publicado == date(2026, 11, 4) and not res.desconocidas
    v = valores("BOE-A-2026-23168")
    assert v[("2026-10", "euribor_12m")] == D("3.239")
    assert v[("2026-10", "euribor_6m")] == D("2.975")
    assert v[("2026-10", "estr_12m")] == D("2.077")  # the €STR section has its own "– Un año."
    assert v[("2026-10", "irs_5a")] == D("3.455")
    assert v[("2026-10", "mibor_12m")] == D("3.239")
    assert len(v) == 12


def test_formato_actual_mitad_de_mes_irph():
    assert valores("BOE-A-2026-21866") == {
        ("2026-09", "irph_entidades"): D("3.189"),
        ("2026-09", "irph_zona_euro"): D("3.600"),
    }


@pytest.mark.parametrize(
    "boe_id, mes, indice, valor",
    [
        ("BOE-A-2025-11062", "2025-05", "euribor_12m", "2.081"),  # «Mayo 2025», without «de»
        ("BOE-A-2022-16188", "2022-09", "estr_3m", "-0.101"),  # header in one cell (colspan), en dash minus
        ("BOE-A-2021-1527", "2021-01", "euribor_12m", "-0.505"),  # «Euríbor a un año» rows
        ("BOE-A-2014-11397", "2014-10", "euribor_12m", "0.338"),  # «Referencia interbancaria a un año»
        ("BOE-A-2016-7978", "2016-07", "irph_entidades", "2.007"),  # three cells per row
        ("BOE-A-2010-921", "2009-12", "irph_entidades", "2.819"),  # bancos / cajas / conjunto
        ("BOE-A-2010-921", "2009-12", "ceca", "4.625"),
        ("BOE-A-2008-17158", "2008-09", "euribor_12m", "5.384"),  # month at the end of a paragraph
        ("BOE-A-2004-11718", "2004-05", "irph_cajas", "3.351"),  # «Mayo 2004:» in parrafo_2
    ],
)
def test_formatos_antiguos(boe_id, mes, indice, valor):
    assert valores(boe_id)[(mes, indice)] == D(valor)


def test_correcciones_de_erratas():
    xmls = {
        i: leer(i)
        for i in (
            "BOE-A-2006-11342",
            "BOE-A-2006-11488",
            "BOE-A-2008-8992",
            "BOE-A-2008-9040",
            "BOE-A-2021-15173",
            "BOE-A-2021-15299",
        )
    }
    resoluciones, problemas = leer_resoluciones(xmls)
    assert problemas == []
    serie = construir_serie(resoluciones, date(2026, 11, 13), {})["series"]
    # The 2006 resolution forgot the month; its correction says «Mayo 2006».
    assert serie["irph_entidades"]["2006-05"]["boe_id"] == "BOE-A-2006-11342"
    # The 2008 correction: «donde dice: «5,148», debe decir: «5,418»».
    assert serie["irph_entidades"]["2008-04"]["valor"] == "5.418"
    # The 2021 correction reprints the table: the later publication wins and records what it replaced.
    agosto = serie["irph_entidades"]["2021-08"]
    assert agosto["valor"] == "1.529" and agosto["boe_id"] == "BOE-A-2021-15299"
    assert agosto["sustituye"] == "BOE-A-2021-15173: 1,450"


def test_sin_la_correccion_falta_el_mes():
    with pytest.raises(FormatoResolucionError, match="mes"):
        parsear_resolucion(leer("BOE-A-2006-11342"))


def test_mes_inferido_queda_anotado():
    res = parsear_resolucion(leer("BOE-A-2023-11879"))
    assert {v.mes for v in res.valores} == {"2023-04"}  # the BOE prints «Abril de 2003»
    assert all(v.nota == MES_INFERIDO["BOE-A-2023-11879"][2] for v in res.valores)
    assert cargar_serie().datos["irph_entidades"]["2023-04"].boe_id == "BOE-A-2023-11879"


def test_rechaza_lo_que_no_es_una_resolucion_de_tipos():
    otro = re.sub(
        rb"<titulo>.*?</titulo>",
        "<titulo>Resolución del Banco de España: cambios del euro</titulo>".encode(),
        leer("BOE-A-2026-23168"),
    )
    with pytest.raises(FormatoResolucionError, match="no es una resolución"):
        parsear_resolucion(otro)
    with pytest.raises(FormatoResolucionError, match="XML ilegible"):
        parsear_resolucion(b"<documento")


def test_fila_desconocida_se_informa_y_no_se_adivina():
    xml = leer("BOE-A-2026-21866").replace(
        "Tipo medio de los préstamos hipotecarios a más de tres años".encode(), b"Un tipo nuevo"
    )
    res = parsear_resolucion(xml)
    assert [v.indice for v in res.valores] == ["irph_zona_euro"]
    assert len(res.desconocidas) == 1 and "Un tipo nuevo" in res.desconocidas[0]


def test_serie_guardada_coincide_con_sus_fuentes():
    """Every fixture value is stored exactly (unless a later publication replaced it) with its BOE id and
    the hash of that XML."""
    serie = cargar_serie()
    xmls = {p.stem: p.read_bytes() for p in RESOLUCIONES.glob("BOE-A-*.xml")}
    resoluciones, problemas = leer_resoluciones(xmls)
    assert problemas == []
    comprobados = 0
    for res in resoluciones:
        assert serie.documentos[res.boe_id]["sha256_xml"] == hashlib.sha256(xmls[res.boe_id]).hexdigest()
        for v in res.valores:
            guardado = serie.valor(v.indice, v.mes)
            assert guardado is not None
            if guardado.boe_id == res.boe_id:
                assert guardado.valor == v.valor and guardado.literal == v.literal
                comprobados += 1
    assert comprobados >= 90


def test_serie_completa_del_euribor():
    serie = cargar_serie()
    meses = sorted(serie.datos["euribor_12m"])
    assert meses[0] <= "2004-01" and meses[-1] >= "2026-10"
    year, month = map(int, meses[0].split("-"))
    esperado = []
    while f"{year:04d}-{month:02d}" <= meses[-1]:
        esperado.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    assert meses == esperado  # no gaps


def test_ultimo_y_anterior():
    serie = cargar_serie()
    ultimo = serie.ultimo("euribor_12m", publicado_hasta=date(2026, 11, 3))
    assert ultimo is not None and ultimo.mes == "2026-09" and ultimo.boe_id == "BOE-A-2026-20519"
    anterior = serie.anterior(ultimo)
    assert anterior is not None and anterior.mes == "2026-08"
    assert serie.ultimo("euribor_12m", publicado_hasta=date(2000, 1, 1)) is None


PAGINA = """<ul>
<li class="resultado-busqueda"><p>Banco de España</p><p>Resolución de 3 de noviembre de 2026, del Banco de España,
por la que se publican determinados tipos de interés oficiales de referencia del mercado hipotecario.</p>
<a href="/diario_boe/txt.php?id=BOE-A-2026-23168">Ir</a></li>
<li class="resultado-busqueda"><p>Resolución del Banco de España por la que se publican los cambios del euro</p>
<a href="/diario_boe/txt.php?id=BOE-A-2026-23170">Ir</a></li>
<li class="resultado-busqueda"><p>Banco de España: tipo de rendimiento interno de la deuda pública, tipo de
referencia</p><a href="/diario_boe/txt.php?id=BOE-A-2026-23171">Ir</a></li>
</ul>"""


def test_busqueda_y_actualizacion_con_fetcher_falso():
    assert ids_en_resultados(PAGINA) == ["BOE-A-2026-23168"]
    pedidas: list[str] = []

    def fetch(url: str) -> bytes:
        pedidas.append(url)
        if "buscar/boe.php" in url:
            return PAGINA.encode()
        assert url == BOE_XML_URL.format(boe_id="BOE-A-2026-23168")
        return leer("BOE-A-2026-23168")

    nuevos = actualizar(fetch, date(2026, 11, 1), date(2026, 11, 13), existentes=set())
    assert list(nuevos) == ["BOE-A-2026-23168"]
    assert actualizar(fetch, date(2026, 11, 1), date(2026, 11, 13), existentes={"BOE-A-2026-23168"}) == {}
    assert all("www.boe.es" in u for u in pedidas)


def test_construir_sobre_una_serie_existente():
    base = {
        "series": {
            "euribor_12m": {"2026-09": {"valor": "3.247", "literal": "3,247", "boe_id": "X", "publicado": "2026-10-02"}}
        },
        "documentos": {"X": {"publicado": "2026-10-02", "sha256_xml": ""}},
    }
    res = parsear_resolucion(leer("BOE-A-2026-23168"))
    nueva = construir_serie([res], date(2026, 11, 13), {"BOE-A-2026-23168": "abc"}, base)
    s = serie_desde_json(nueva)
    assert s.valor("euribor_12m", "2026-09").boe_id == "X"
    assert s.valor("euribor_12m", "2026-10").valor == D("3.239")
    assert set(nueva["documentos"]) == {"X", "BOE-A-2026-23168"}
