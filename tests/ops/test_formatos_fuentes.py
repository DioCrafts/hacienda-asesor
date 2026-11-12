"""The sources brought under the format watch after the BOE: CENDOJ/Supremo, AEAT, autonomic codes and Docling,
the fiscal calendar, the seed CLIs (TEAC listing, DGT, boe_seed) and the LLM price table.

For each one: version 1 of its spec (if it has one) reads exactly like the code it replaced; instrumenting the
real reader does not change what it returns or writes; its canaries pass on a good payload and catch a drifted
one. No network: the CENDOJ is never contacted (see its reuse conditions), the BOE pages are fixtures captured
on 12-11-2026, and the AEAT and DGT pages are synthetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import formatos_util as util
from langchain_core.documents import Document
import legacy_parsers as legacy
import pytest
from scrapy.http import HtmlResponse, Request, Response
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.python.failure import Failure

from hacienda_gpt import settings
from hacienda_gpt.cli import boe_seed, dgt_seed, supremo_seed, teac_seed
from hacienda_gpt.cli import formatos as cli_formatos
from hacienda_gpt.crawler import cendoj, crawlers, teac
from hacienda_gpt.crawler.boe_consolidado import BOECCAACrawler
from hacienda_gpt.ops import formatos, formatos_agente, formatos_calendario, vigia
from hacienda_gpt.ops.formatos_corpus import RecuentoCorpus
from hacienda_gpt.tax import calendario

HOY = date(2026, 11, 12)
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
CENDOJ = util.FIXTURES / "cendoj"
CALENDARIO_BOE = "calendario_boe_20261112.json"
SEDE = "https://sede.agenciatributaria.gob.es/Sede/irpf.html"


@pytest.fixture
def vigilancia(tmp_path, monkeypatch):
    """The watch switched on, writing under ``tmp_path``; no channels, no healthcheck, no network."""
    monkeypatch.setattr(settings, "FORMAT_WATCH", True)
    monkeypatch.setattr(settings, "FORMAT_STATE_PATH", str(tmp_path / "estado.json"))
    monkeypatch.setattr(settings, "FORMAT_DIR", str(tmp_path / "formatos"))
    monkeypatch.setattr(settings, "JOBS_STATE_PATH", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")
    monkeypatch.setattr(settings, "FORMAT_AUTO_APPLY", False)
    monkeypatch.setattr(cli_formatos, "_canales", lambda: [])
    monkeypatch.setattr(cli_formatos, "_hoy", lambda: HOY)
    monkeypatch.setattr(vigia.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("no network in tests"))
    return tmp_path


def _lectura(clave: str, crudo: str, contexto: str = "") -> formatos.Lectura:
    fuente = formatos.fuente_de(clave)
    reglas = formatos.reglas_de(clave, fuente.reglas) if fuente.reglas is not None else None
    return formatos.evaluar(fuente, reglas, crudo, contexto)[1]


def _graves(clave: str, crudo: str, contexto: str = "") -> set[str]:
    return {f.canario for f in _lectura(clave, crudo, contexto).fallos if f.gravedad == "grave"}


def _estado(clave: str) -> str:
    return {e.fuente: e.estado for e in formatos.resumen()}[clave]


def _ultima(clave: str) -> formatos.Observacion:
    return formatos.observaciones_de(formatos.leer_estado().get(clave))[-1]


def _sin_hora(datos: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in datos.items() if k not in ("ingested_at", "generado_en")}


# --------------------------------------------------------------------------- #
# CENDOJ / Tribunal Supremo
# --------------------------------------------------------------------------- #


def _sentencias() -> list[str]:
    from tests.doctrine.test_supremo import _sentencia_html

    return [
        (CENDOJ / "sentencia_tributaria.html").read_text(encoding="utf-8"),
        (CENDOJ / "sentencia_no_tributaria.html").read_text(encoding="utf-8"),
        _sentencia_html(),
        _sentencia_html(seccion_cabecera=None, id_cendoj=None, fecha="03-02-2015", roj="STS   77/2015"),
        "<html><body><p>ECLI:ES:TS:2020:9 Sala 3ª. Ponente: X Y.</p></body></html>",
        "<html><body></body></html>",
        "",
    ]


@pytest.mark.parametrize("indice", range(7))
def test_cendoj_v1_reads_like_the_old_parser(indice: int) -> None:
    html = _sentencias()[indice]

    nuevo = cendoj.parse_cendoj_document(html, source_url="https://www.poderjudicial.es/x")
    viejo = legacy.parse_cendoj_document(html, source_url="https://www.poderjudicial.es/x")

    assert list(nuevo) == list(viejo) and _sin_hora(nuevo) == _sin_hora(viejo)


def test_cendoj_v1_patterns_are_the_old_expressions() -> None:
    reglas = formatos.reglas_de("cendoj_documento", cendoj.ReglasCendoj)

    assert reglas.patron_roj == "(?i)" + legacy._CENDOJ_ROJ_PATTERN.pattern
    assert reglas.patron_ecli == "(?i)" + legacy._CENDOJ_ECLI_PATTERN.pattern
    assert reglas.patron_fecha == legacy._CENDOJ_DATE_PATTERN.pattern
    assert reglas.patron_sala == "(?i)" + legacy._CENDOJ_SALA_PATTERN.pattern
    assert (reglas.texto_xpath, reglas.etiqueta_ponente) == ("//body//text()", "Ponente")


def test_cendoj_canaries() -> None:
    bueno = (CENDOJ / "sentencia_tributaria.html").read_text(encoding="utf-8")

    assert _graves("cendoj_documento", bueno) == set()
    # The portal answers with its JavaScript shell instead of the ruling.
    assert _graves("cendoj_documento", "<html><body><div id='app'></div><script>x()</script></body></html>") == {
        "pagina_vacia"
    }
    # Identifiers printed another way: the ruling has no ROJ and no ECLI any more.
    assert "identificadores" in _graves("cendoj_documento", bueno.replace("ROJ:", "Roj -").replace("ECLI:", "Ecli -"))
    # Dates in words: no date, so the cassation regime cannot be decided.
    assert _graves("cendoj_documento", bueno.replace("15/03/2024", "15 de marzo de 2024")) == {"fecha"}


def test_cendoj_crawler_reports_each_ruling_and_writes_the_same_files(vigilancia, tmp_path, monkeypatch) -> None:
    html = (CENDOJ / "sentencia_tributaria.html").read_text(encoding="utf-8")
    url = "https://www.poderjudicial.es/search/AN/openDocument/abc/20240315"

    def leer(carpeta: Path) -> dict[str, Any]:
        spider = cendoj.CENDOJCrawler(folder=str(carpeta), urls=url, snapshot_date="2026-11-12")
        spider.parse_document(HtmlResponse(url=url, body=html.encode(), encoding="utf-8"), slot=0)
        return {p.name: _sin_hora(json.loads(p.read_text())) for p in Path(spider.folder).glob("*.json")}

    con_vigilancia = leer(tmp_path / "a")
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    sin_vigilancia = leer(tmp_path / "b")

    assert con_vigilancia == sin_vigilancia and list(con_vigilancia) == ["STS_1234_2024.json"]
    assert _estado("cendoj_documento") == "ok" and _ultima("cendoj_documento").contexto == url
    # Only the trimmed start of the ruling is kept, without scripts.
    assert "ROJ: STS 1234/2024" in formatos.leer_muestra("cendoj_documento", "buena").texto


def _directorio_supremo(tmp_path: Path, n: int = 4, **kwargs: Any) -> Path:
    from tests.doctrine.test_supremo import _sentencia_html

    origen = tmp_path / "origen"
    origen.mkdir()
    for i in range(n):
        (origen / f"s{i}.html").write_text(_sentencia_html(roj=f"STS {100 + i}/2021", **kwargs), encoding="utf-8")
    return origen


def test_supremo_seed_reports_without_changing_its_manifest(vigilancia, tmp_path, monkeypatch) -> None:
    origen = _directorio_supremo(tmp_path)

    def ejecutar(salida: Path) -> dict[str, Any]:
        result = CliRunner().invoke(supremo_seed.cli, ["--from-dir", str(origen), "--output-dir", str(salida)])
        assert result.exit_code == 0, result.output
        manifiesto = json.loads((salida / supremo_seed.MANIFEST_NAME).read_text())
        for doc in manifiesto["documentos"]:
            doc["origen"] = Path(doc["origen"]).name
        return _sin_hora(manifiesto) | {"from_dir": None}

    con_vigilancia = ejecutar(tmp_path / "a")
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    sin_vigilancia = ejecutar(tmp_path / "b")

    assert con_vigilancia == sin_vigilancia and con_vigilancia["totales"]["conservadas"] == 4
    assert _estado("cendoj_documento") == "ok" and _estado("supremo_lote") == "ok"
    lote = _ultima("supremo_lote").metricas
    assert lote["analizadas"] == 4 and lote["sala_tercera_con_seccion"] == 4 and lote["conservadas"] == 4


def test_supremo_lote_notices_a_header_that_moved(vigilancia, tmp_path) -> None:
    # "Sección: 2" and the Id Cendoj gone, the title spelled another way: no ruling has a section any more,
    # so all of them are discarded — exactly like a run with nothing of interest.
    origen = _directorio_supremo(tmp_path, seccion_cabecera=None, id_cendoj=None, seccion_titulo="Secc. 2.ª")

    result = CliRunner().invoke(supremo_seed.cli, ["--from-dir", str(origen), "--output-dir", str(tmp_path / "s")])

    assert result.exit_code == 0 and "0 conservadas" in result.output
    assert _estado("supremo_lote") == "rota"
    assert [f.canario for f in _ultima("supremo_lote").fallos] == ["seccion"]


def test_cendoj_drift_can_be_adapted_as_data(vigilancia) -> None:
    bueno = (CENDOJ / "sentencia_tributaria.html").read_text(encoding="utf-8")
    nuevo = bueno.replace("ROJ: STS", "ROJ - STS")
    assert formatos.observar("cendoj_documento", bueno, contexto="a", ahora=T0).ok
    # Only the ECLI is left: the ruling would be stored under another name. A warning, and something to adapt.
    assert [f.canario for f in formatos.observar("cendoj_documento", nuevo, contexto="b").fallos] == ["roj"]
    v1 = formatos.spec_incorporada("cendoj_documento").reglas

    # The new separator next to the old one: the old page reads the same, the new one is understood.
    bien = formatos.ensayar("cendoj_documento", {**v1, "patron_roj": r"(?i)\bROJ\s*[:\-]\s*(STS\s*\d+/\d{4})\b"})
    assert bien.ok, bien.motivos
    # A pattern that reads the new page by matching any number at all does not reproduce the old extraction.
    assert not formatos.ensayar("cendoj_documento", {**v1, "patron_roj": r"(\d+/\d{4})"}).ok


# --------------------------------------------------------------------------- #
# AEAT sede
# --------------------------------------------------------------------------- #


def _pagina_sede() -> str:
    return util.leer("aeat_sede_irpf_sintetica.html")


def test_aeat_v1_follows_the_same_links_as_the_old_crawler() -> None:
    reglas = formatos.reglas_de("aeat_sede", crawlers.ReglasSedeAeat)
    html = _pagina_sede()

    assert (
        list(reglas.rutas_permitidas)
        == legacy._AEAT_ALLOWED_PATHS
        == crawlers.AgenciaTributariaWebCrawler.allowed_paths
    )
    nuevos = crawlers.extraer_enlaces(reglas, html, SEDE)
    assert nuevos == legacy.aeat_links_followed(SEDE, html)
    assert nuevos == [
        "https://sede.agenciatributaria.gob.es/Sede/irpf/campana-renta.html",
        "https://sede.agenciatributaria.gob.es/Sede/irpf/tributacion-familiar.html",
        "https://sede.agenciatributaria.gob.es/Sede/iva/declaraciones.html",
        "https://sede.agenciatributaria.gob.es/Sede/colaborar-agencia-tributaria/modelos-100-199.html",
        "https://sede.agenciatributaria.gob.es/Sede/ayuda/manuales-videos-folletos/manuales-practicos/irpf.html",
        # The in-page anchor resolves to the page itself, which is under /Sede/irpf: the old crawler followed it.
        "https://sede.agenciatributaria.gob.es/Sede/irpf.html#contenido",
    ]
    # The sample keeps the page address in a <base>: the links replay the same without it.
    assert crawlers.extraer_enlaces(reglas, f'<base href="{SEDE}">' + html) == nuevos


def test_aeat_crawler_reports_seed_pages_without_changing_what_it_follows(vigilancia, tmp_path, monkeypatch) -> None:
    def rastrear(carpeta: Path) -> list[str]:
        spider = crawlers.AgenciaTributariaWebCrawler(folder=str(carpeta), snapshot_date="2026-11-12")
        respuesta = HtmlResponse(url=SEDE, body=_pagina_sede().encode(), encoding="utf-8")
        return [request.url for request in spider.parse(respuesta)]

    con_vigilancia = rastrear(tmp_path / "a")
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    assert con_vigilancia == rastrear(tmp_path / "b") and len(con_vigilancia) == 6
    monkeypatch.setattr(settings, "FORMAT_WATCH", True)

    assert _estado("aeat_sede") == "ok"
    # A reorganised sede: the page is there, none of its links is under a known path.
    reorganizada = _pagina_sede().replace('href="/Sede/', 'href="/Sede/es/').replace("/Sede/iva/", "/Sede/es/iva/")
    nueva = "https://sede.agenciatributaria.gob.es/Sede/es/irpf.html"
    spider = crawlers.AgenciaTributariaWebCrawler(folder=str(tmp_path / "c"), snapshot_date="2026-11-12")
    spider._semillas.add(nueva)
    respuesta = HtmlResponse(
        url=nueva, body=reorganizada.replace('href="#contenido"', 'href="#c"').encode(), encoding="utf-8"
    )
    assert list(spider.parse(respuesta)) == []
    assert _estado("aeat_sede") == "degradada" and [f.canario for f in _ultima("aeat_sede").fallos] == ["enlaces"]

    # The agent may propose the new paths NEXT TO the old ones; the gate replays both pages.
    v1 = formatos.spec_incorporada("aeat_sede").reglas
    nuevas = {**v1, "rutas_permitidas": [*v1["rutas_permitidas"], "/Sede/es/irpf", "/Sede/es/iva"]}
    assert formatos.ensayar("aeat_sede", nuevas).ok
    sustituidas = {**v1, "rutas_permitidas": ["/Sede/es/irpf", "/Sede/es/iva"]}
    assert not formatos.ensayar("aeat_sede", sustituidas).ok


def _fallo_http(url: str, status: int, **cb_kwargs: Any) -> Failure:
    failure = Failure(HttpError(Response(url=url, status=status), "Ignoring non-200 response"))
    failure.request = Request(url, cb_kwargs=cb_kwargs)  # type: ignore[attr-defined]
    return failure


def test_aeat_run_reports_pages_and_missing_seed_pages(vigilancia, tmp_path) -> None:
    spider = crawlers.AgenciaTributariaWebCrawler(folder=str(tmp_path), snapshot_date="2026-11-12")
    list(spider.parse(HtmlResponse(url=SEDE, body=_pagina_sede().encode(), encoding="utf-8")))
    spider._semilla_fallida(_fallo_http("https://sede.agenciatributaria.gob.es/Sede/iva.html", 404))

    spider.closed("finished")

    fallos = _ultima("aeat_rastreo").fallos
    assert [(f.canario, f.gravedad) for f in fallos] == [("semillas", "leve")] and "iva.html" in fallos[0].detalle
    # Most entry pages gone: grave.
    spider = crawlers.AgenciaTributariaWebCrawler(folder=str(tmp_path), snapshot_date="2026-11-12")
    for url in spider.urls[:7]:
        spider._semilla_fallida(_fallo_http(url, 404))
    spider.closed("finished")
    assert {f.canario for f in _ultima("aeat_rastreo").fallos if f.gravedad == "grave"} == {"paginas", "semillas"}


def test_a_run_that_reached_nothing_is_not_judged(vigilancia, tmp_path) -> None:
    spider = crawlers.AgenciaTributariaWebCrawler(folder=str(tmp_path), snapshot_date="2026-11-12")
    for url in spider.urls:
        failure = Failure(ConnectionError("sin red"))
        failure.request = Request(url)  # type: ignore[attr-defined]
        spider._semilla_fallida(failure)
    spider.closed("finished")
    crawlers.AgenciaTributariaPDFCrawler(folder=str(tmp_path / "p"), snapshot_date="2026-11-12").closed("finished")

    assert _estado("aeat_rastreo") == _estado("aeat_pdf") == "sin_datos"


def test_aeat_pdf_run_notices_viewers_instead_of_pdfs(vigilancia, tmp_path) -> None:
    from tests.test_pdf_crawler_saves import _FakeResponse

    spider = crawlers.AgenciaTributariaPDFCrawler(folder=str(tmp_path), snapshot_date="2026-01-01")
    spider.parse_pdf(_FakeResponse("https://x/a.pdf", b"%PDF-1.7 a"))
    spider.parse_pdf(_FakeResponse("https://x/b.pdf", b"%PDF-1.7 a"))
    spider.closed("finished")
    assert _estado("aeat_pdf") == "ok" and _ultima("aeat_pdf").metricas["duplicados"] == 1

    spider = crawlers.AgenciaTributariaPDFCrawler(folder=str(tmp_path / "2"), snapshot_date="2026-01-01")
    for nombre in "abc":
        spider.parse_pdf(_FakeResponse(f"https://x/{nombre}.pdf", b"<html>visor</html>"))
    spider.closed("finished")
    assert _estado("aeat_pdf") == "degradada" and [f.canario for f in _ultima("aeat_pdf").fallos] == ["no_pdf"]


# --------------------------------------------------------------------------- #
# The BOE's autonomic codes: download (PDF) and conversion (Docling)
# --------------------------------------------------------------------------- #


def _ccaa(tmp_path: Path) -> BOECCAACrawler:
    return BOECCAACrawler(folder=str(tmp_path), ccaa="andalucia,aragon,madrid", snapshot_date="2026-11-12")


def _respuesta_pdf(body: bytes, status: int = 200) -> Response:
    return Response(
        url="https://www.boe.es/biblioteca_juridica/codigos/abrir_pdf.php?fich=x.pdf", status=status, body=body
    )


def test_ccaa_pdf_run_flags_an_error_page_and_a_moved_file(vigilancia, tmp_path) -> None:
    spider = _ccaa(tmp_path)
    spider.parse_pdf(_respuesta_pdf(b"%PDF-1.4 codigo"), "andalucia", "cat")
    spider.closed("finished")
    assert _estado("boe_ccaa_pdf") == "ok"

    spider = _ccaa(tmp_path)
    spider.parse_pdf(_respuesta_pdf(b"%PDF-1.4 codigo"), "andalucia", "cat")
    spider.parse_pdf(_respuesta_pdf(b"<html><body>Documento no disponible</body></html>"), "madrid", "cat")
    spider.handle_error(_fallo_http("https://www.boe.es/x", 404, ccaa_key="aragon", catalog_url="cat"))
    spider.closed("finished")
    fallos = {(f.canario, f.gravedad) for f in _ultima("boe_ccaa_pdf").fallos}
    # Madrid answered HTML; Aragón's file name (not verified in the catalogue) is gone.
    assert fallos == {("no_pdf", "grave"), ("fichero_no_verificado", "leve")}
    assert "madrid" in formatos.leer_muestra("boe_ccaa_pdf", "mala").texto


def test_ccaa_pdf_run_without_network_records_nothing(vigilancia, tmp_path) -> None:
    spider = _ccaa(tmp_path)
    failure = Failure(ConnectionError("sin red"))
    failure.request = Request("https://www.boe.es/x", cb_kwargs={"ccaa_key": "madrid"})  # type: ignore[attr-defined]
    spider.handle_error(failure)

    spider.closed("finished")

    assert _estado("boe_ccaa_pdf") == "sin_datos"


TEXTO_CODIGO = (
    "# Ley 5/2021, de tributos cedidos\n\n## Artículo 1. Objeto\n\nLa presente ley regula los tributos cedidos."
)


def _codigo(tmp_path: Path, ccaa: str = "madrid", kb: int = 10) -> str:
    path = tmp_path / "html" / "2026-11-12" / "boe_ccaa" / ccaa / f"codigo_{ccaa}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4" + b"x" * (kb * 1024))
    return str(path)


def test_ccaa_text_canaries(vigilancia, tmp_path) -> None:
    recuento = RecuentoCorpus(tmp_path / "html")
    recuento.anotar(_codigo(tmp_path, "madrid"), [Document(page_content=TEXTO_CODIGO)])
    recuento.anotar(_codigo(tmp_path, "galicia"), [])
    recuento.anotar(_codigo(tmp_path, "murcia"), [Document(page_content="Texto corrido sin estructura alguna " * 20)])
    recuento.anotar(str(tmp_path / "html" / "a.html"), [Document(page_content="x")])
    recuento.informar("completa")

    fallos = {f.canario: f for f in _ultima("boe_ccaa_texto").fallos}
    assert set(fallos) == {"sin_texto", "sin_encabezados"}
    assert "galicia" in fallos["sin_texto"].detalle and "murcia" in fallos["sin_encabezados"].detalle
    assert _ultima("corpus_docling").metricas == {"ficheros": 4.0, "fragmentos_por_fichero": 0.75}
    # The sample is the start of each code's text: enough to see what Docling produced.
    assert "Artículo 1. Objeto" in formatos.leer_muestra("boe_ccaa_texto", "mala").texto


def test_ccaa_text_yield_drop_against_the_recent_median(vigilancia, tmp_path) -> None:
    texto = TEXTO_CODIGO + "\n" + "Artículo 2. Hecho imponible.\n" + "x" * 3000
    for _ in range(5):
        recuento = RecuentoCorpus()
        recuento.anotar(_codigo(tmp_path), [Document(page_content=texto)])
        recuento.informar()
    assert _estado("boe_ccaa_texto") == "ok"

    recuento = RecuentoCorpus()
    recuento.anotar(_codigo(tmp_path), [Document(page_content=texto[:400])])
    recuento.informar()

    assert [f.canario for f in _ultima("boe_ccaa_texto").fallos] == ["caida_caracteres_por_kb"]


def test_the_ingest_reports_zero_chunk_files_without_changing_the_manifest(vigilancia, tmp_path, monkeypatch) -> None:
    from hacienda_gpt.processor import document_loader as dl
    from hacienda_gpt.processor import manifest as mf
    from tests.test_document_loader_incremental import _DeterministicEmbed, _docs_for

    contenido = tmp_path / "html"
    (contenido / "a.html").parent.mkdir(parents=True, exist_ok=True)
    (contenido / "a.html").write_text("<html>a</html>")
    vacio = _codigo(tmp_path, "madrid")

    def fake_stream(self, files):
        for f in files:
            yield f, ([] if f == vacio else _docs_for(f, 2))

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", fake_stream)

    def indexar(salida: Path) -> dict[str, int]:
        dl.DocumentProcessor(
            embeddings=_DeterministicEmbed(),
            content_dir=str(contenido),
            output_dir=str(salida),
            num_workers=1,
            embedder_model="test/embedder",
            embedder_dim=8,
        ).process_documents()
        manifiesto = mf.load_manifest(salida)
        assert manifiesto is not None
        return {clave: entrada.n_chunks for clave, entrada in manifiesto.files.items()}

    con_vigilancia = indexar(tmp_path / "faiss_a")
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    assert con_vigilancia == indexar(tmp_path / "faiss_b") == {"2026-11-12/boe_ccaa/madrid/codigo_madrid.pdf": 0, "a.html": 2}  # fmt: skip

    assert _estado("boe_ccaa_texto") == "rota" and [f.canario for f in _ultima("boe_ccaa_texto").fallos] == ["sin_texto"]  # fmt: skip
    assert _ultima("corpus_docling").contexto == "completa"


# --------------------------------------------------------------------------- #
# Fiscal calendar: coherence of the data
# --------------------------------------------------------------------------- #


def _calendario(**cambios: Any) -> str:
    data = json.loads(calendario.RUTA_CALENDARIO.read_text(encoding="utf-8"))
    for ruta, valor in cambios.items():
        nodo = data
        *pasos, ultimo = ruta.split("__")
        for paso in pasos:
            nodo = nodo[int(paso)] if paso.isdigit() else nodo[paso]
        nodo[int(ultimo) if ultimo.isdigit() else ultimo] = valor
    return json.dumps(data, ensure_ascii=False)


def _fallos_calendario(crudo: str, hoy: date = HOY) -> dict[str, str]:
    lectura = _lectura("calendario_fiscal", crudo, hoy.isoformat())
    return {f.canario: f.gravedad for f in lectura.fallos}


def test_calendar_data_is_coherent_today() -> None:
    assert _fallos_calendario(calendario.RUTA_CALENDARIO.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize(
    "cambios, esperado",
    [
        # 30 February: calendario.py skips it without a word, every year.
        ({"obligaciones__2__plazos__0__mes": 2, "obligaciones__2__plazos__0__dia": 30}, {"fecha_imposible": "grave"}),
        ({"obligaciones__2__modelo": "3033"}, {"modelo_desconocido": "grave"}),
        ({"consultado_el": "2027-01-01"}, {"consultado_el": "grave"}),
        ({"consultado_el": "2025-06-01"}, {"antiguedad": "leve"}),
        ({"obligaciones__3__plazos__1__periodo": "2T"}, {"periodo": "leve"}),
        ({"obligaciones__3__id": "iva_trimestral"}, {"duplicados": "grave"}),
    ],
)
def test_calendar_canaries_catch_bad_data(cambios: dict[str, Any], esperado: dict[str, str]) -> None:
    assert _fallos_calendario(_calendario(**cambios)) == esperado


def test_a_deadline_that_never_falls_this_year_or_next(monkeypatch) -> None:
    # 29 February only exists in leap years: 2027 has no deadline at all for that obligation.
    crudo = _calendario(obligaciones__4__plazos__0__mes=2, obligaciones__4__plazos__0__dia=29)

    fallos = _lectura("calendario_fiscal", crudo, "2027-03-01").fallos

    assert {(f.canario, f.gravedad) for f in fallos} >= {("fecha_imposible", "grave"), ("anio_sin_plazo", "grave")}


def test_an_unverified_deadline_close_to_falling_due_is_reported() -> None:
    fallos = _lectura("calendario_fiscal", calendario.RUTA_CALENDARIO.read_text(), "2027-04-10").fallos

    assert [(f.canario, f.gravedad) for f in fallos] == [("sin_verificar", "leve")]
    assert "30/06/2027" in fallos[0].detalle and "orden ministerial" in fallos[0].detalle


def test_check_records_the_calendar_and_the_reader_is_unchanged(vigilancia) -> None:
    antes = calendario.proximos_vencimientos({"alta_actividad_economica": True}, hoy=HOY)

    result = CliRunner().invoke(cli_formatos.cli, ["check", "--fail-on-problem"])

    assert result.exit_code == 0, result.output
    assert {f["fuente"]: f["estado"] for f in json.loads(result.output)["fuentes"]} == {"calendario_fiscal": "ok"}
    assert calendario.proximos_vencimientos({"alta_actividad_economica": True}, hoy=HOY) == antes


# --------------------------------------------------------------------------- #
# Fiscal calendar: contrast with the BOE
# --------------------------------------------------------------------------- #


def test_the_bundled_quotes_are_in_the_bundled_law_texts() -> None:
    assert _lectura("calendario_boe", util.leer(CALENDARIO_BOE), HOY.isoformat()).fallos == []


def test_article_text_is_cut_from_the_real_markup() -> None:
    nbsp = util.leer("boe_bloque_BOE-A-2015-10565_30.html")
    rirpf = util.leer("boe_bloque_BOE-A-2007-6820_111.html")

    texto = formatos_calendario.texto_articulo(nbsp, "Artículo 30.")
    assert texto is not None and texto.splitlines()[1] == "Artículo 30. Cómputo de plazos."
    assert (
        "5. Cuando el último día del plazo sea inhábil, se entenderá prorrogado al primer día hábil siguiente." in texto
    )
    assert "b) Cuarto trimestre, entre el día 1 y el 30 del mes de enero." in (
        formatos_calendario.texto_articulo(rirpf, "Artículo 111.") or ""
    )
    assert formatos_calendario.texto_articulo(rirpf, "Artículo 11.") is None


def test_a_long_payload_is_trimmed_into_valid_json_that_still_reads() -> None:
    carga = json.loads(util.leer(CALENDARIO_BOE))
    relleno = "\n".join(f"Se modifica por la Ley {n}/2030." for n in range(3000))
    carga["BOE-A-1992-28925"]["Artículo 71."] += "\n" + relleno
    crudo = json.dumps(carga, ensure_ascii=False, indent=1)
    assert len(crudo) > formatos.MAX_MUESTRA_CHARS

    recorte = formatos_calendario.recortar_contraste(crudo)

    assert len(recorte) <= formatos.MAX_MUESTRA_CHARS and "Se modifica por la Ley 2999" not in recorte
    assert _lectura("calendario_boe", recorte, HOY.isoformat()).fallos == []
    assert formatos_calendario.recortar_contraste(util.leer(CALENDARIO_BOE)) == util.leer(CALENDARIO_BOE)


def _carga_con(cambio: tuple[str, str]) -> str:
    return util.leer(CALENDARIO_BOE).replace(*cambio)


def _diagnosticable(vigilancia_dir: Path, nuevo: str) -> None:
    assert formatos.observar("calendario_boe", util.leer(CALENDARIO_BOE), contexto="2026-11-12", ahora=T0).ok
    assert not formatos.observar("calendario_boe", nuevo, contexto="2026-11-13").ok


def test_a_changed_deadline_cannot_be_adapted_away(vigilancia) -> None:
    # The BOE now gives fifteen days for the quarterly VAT return.
    nuevo = _carga_con(("los veinte primeros días naturales", "los quince primeros días naturales"))
    assert _graves("calendario_boe", nuevo) == {"cita"}
    _diagnosticable(vigilancia, nuevo)
    v1 = formatos.spec_incorporada("calendario_boe").reglas
    iva = v1["contrastes"][0]
    nueva_cita = [iva["citas"][0], iva["citas"][0].replace("veinte", "quince")]

    ensayo = formatos.ensayar(
        "calendario_boe", {"contrastes": [{**iva, "citas": [nueva_cita, iva["citas"][1]]}, *v1["contrastes"][1:]]}
    )

    # Quoting the new wording does not help: calendario.json still says the 20th, which the law no longer says.
    assert not ensayo.ok and any("calendario.json fija «iva_trimestral»" in motivo for motivo in ensayo.motivos)


def test_a_reworded_article_with_the_same_deadline_is_adapted_by_adding_the_new_wording(vigilancia) -> None:
    viejo = "b) Cuarto trimestre, entre el día 1 y el 30 del mes de enero."
    nuevo_texto = "b) El cuarto trimestre, del 1 al 30 de enero."
    nuevo = _carga_con((viejo, nuevo_texto))
    _diagnosticable(vigilancia, nuevo)
    v1 = formatos.spec_incorporada("calendario_boe").reglas
    pago = v1["contrastes"][1]

    alternativa = {**pago, "citas": [pago["citas"][0], [viejo, nuevo_texto]]}
    sustituida = {**pago, "citas": [pago["citas"][0], nuevo_texto]}
    otras = v1["contrastes"]

    assert formatos.ensayar("calendario_boe", {"contrastes": [otras[0], alternativa, *otras[2:]]}).ok
    # Replacing the old wording breaks the known-good sample.
    assert not formatos.ensayar("calendario_boe", {"contrastes": [otras[0], sustituida, *otras[2:]]}).ok


def test_the_calendar_proposal_is_never_applied_automatically(vigilancia) -> None:
    viejo = "b) Cuarto trimestre, entre el día 1 y el 30 del mes de enero."
    nuevo_texto = "b) El cuarto trimestre, del 1 al 30 de enero."
    _diagnosticable(vigilancia, _carga_con((viejo, nuevo_texto)))
    v1 = formatos.spec_incorporada("calendario_boe").reglas
    pago = v1["contrastes"][1]
    reglas = {"contrastes": [v1["contrastes"][0], {**pago, "citas": [pago["citas"][0], [viejo, nuevo_texto]]}, *v1["contrastes"][2:]]}  # fmt: skip
    proveedor = util.ProveedorGuionizado(
        [
            {
                "que_ha_cambiado": "El art. 111.1.b) RIRPF se ha reescrito sin cambiar el plazo.",
                "causa": "formato_cambiado",
                "evidencia": nuevo_texto,
                "adaptable_con_datos": True,
                "reglas_json": json.dumps(reglas, ensure_ascii=False),
                "justificacion": "Se añade la redacción nueva como alternativa; sigue diciendo el día 30 de enero.",
            },
            {"conforme": True, "objeciones": [], "riesgo": "Bajo."},
        ]
    )

    propuesta = formatos_agente.diagnosticar("calendario_boe", proveedor, auto_aplicar=True)

    assert (propuesta.tipo, propuesta.estado) == ("spec", "propuesta")
    assert formatos.spec_activa("calendario_boe").version == 1
    assert formatos.aplicar(propuesta.id).spec_version_aplicada == 2


def _pagina_de_ley(boe_id: str) -> str:
    """A consolidated-law page rebuilt from the articles captured in the calendar fixture."""
    carga = json.loads(util.leer(CALENDARIO_BOE))
    bloques = "".join(
        f'<div class="bloque"><h5 class="articulo">{texto.splitlines()[1]}</h5>'
        + "".join(f'<p class="parrafo">{linea}</p>' for linea in texto.splitlines()[2:])
        + "</div>"
        for texto in carga[boe_id].values()
    )
    return f'<html><body><div id="textoxslt">{bloques}</div></body></html>'


def test_probe_reads_the_calendar_laws_from_the_boe(vigilancia, tmp_path) -> None:
    pedidas: list[str] = []

    def descargar(url: str, _accept: str) -> str | None:
        pedidas.append(url)
        return _pagina_de_ley(url.rsplit("=", 1)[1])

    errores = formatos_calendario.sondear_calendario(HOY, cli_formatos.con_memoria(descargar))

    assert errores == [] and len(pedidas) == len(set(pedidas)) == 5
    assert all(url.startswith("https://www.boe.es/buscar/act.php?id=") for url in pedidas)
    assert _estado("calendario_boe") == "ok" and _estado("calendario_fiscal") == "ok"

    def caida(_url: str, _accept: str) -> str | None:
        raise ConnectionError("sin red")

    errores = formatos_calendario.sondear_calendario(HOY, caida)
    # Not being able to read the law is an error to report, not "the law changed".
    assert len(errores) == 5 and _estado("calendario_boe") == "ok"


def test_probe_command_covers_the_boe_formats_and_the_calendar(vigilancia, monkeypatch) -> None:
    from tests.ops.test_formatos import SUMARIO, _descargas

    boe = _descargas(util.leer(SUMARIO))
    lirpf = util.leer("boe_consolidado_BOE-A-2006-20764_recorte.html")
    pedidas: list[str] = []

    def descargar(url: str, accept: str) -> str | None:
        pedidas.append(url)
        if "act.php?id=" not in url:
            return boe(url, accept)
        boe_id = url.rsplit("=", 1)[1]
        # The IRPF law is both the format probe's sample and the source of a deadline: one page, read once.
        return lirpf + _pagina_de_ley(boe_id) if boe_id == "BOE-A-2006-20764" else _pagina_de_ley(boe_id)

    monkeypatch.setattr(cli_formatos, "_descargar", descargar)

    result = CliRunner().invoke(cli_formatos.cli, ["probe", "--fail-on-problem"])

    assert result.exit_code == 0, result.output
    salida = json.loads(result.output)
    assert salida["estados"] == {clave: "ok" for clave in cli_formatos.SONDEADAS} and salida["errores"] == []
    assert pedidas.count("https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764") == 1


# --------------------------------------------------------------------------- #
# Seeds: TEAC listing and criterio, DGT, boe_seed
# --------------------------------------------------------------------------- #


def test_teac_v1_templates_are_the_old_xpath() -> None:
    reglas = formatos.reglas_de("teac_criterio", teac.ReglasTeac)

    assert reglas.plantillas == teac.PLANTILLAS_V1
    plantillas = [p.replace("{alias}", "fecha") for grupo in reglas.plantillas for p in grupo]
    assert [p for p in plantillas if p.startswith("//dt")] == [
        "//dt[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', "
        "'abcdefghijklmnñopqrstuvwxyzáéíóú'))='fecha']/following-sibling::dd[1]//text()"
    ]
    with pytest.raises(ValueError):
        teac.ReglasTeac(**{**formatos.spec_incorporada("teac_criterio").reglas, "plantillas": [["//dt[@x='y']"]]})


def test_teac_listing_v1_and_canaries(vigilancia) -> None:
    from tests.test_teac_seed_cli import _EMPTY_LIST_PAGE, _LIST_PAGE_1, _LIST_PAGE_2

    reglas = formatos.reglas_de("teac_listado", teac_seed.ReglasListadoTeac)
    assert reglas.patron_enlace == "(?i)" + legacy._TEAC_CRITERIO_LINK_RE.pattern
    for pagina in (_LIST_PAGE_1, _LIST_PAGE_2, _EMPTY_LIST_PAGE):
        assert teac_seed.ids_de_pagina(reglas, pagina) == legacy.teac_ids_de_pagina(pagina)

    assert _graves("teac_listado", _LIST_PAGE_1, "01/01/2024..31/12/2024") == set()
    # Links in another form on the first page of a whole year: no criterio at all.
    otra_forma = _LIST_PAGE_1.replace("criterio.aspx?id=", "Criterio/Ver/")
    assert _graves("teac_listado", otra_forma, "01/01/2024..31/12/2024") == {"enlaces"}
    # A short range may legitimately be empty: only a warning.
    assert _graves("teac_listado", _EMPTY_LIST_PAGE, "01/03/2024..05/03/2024") == set()


def test_teac_seed_reports_the_listing_and_each_criterio(vigilancia, tmp_path) -> None:
    from tests.test_teac_seed_cli import _CRITERIO_VALID, _EMPTY_LIST_PAGE, _LIST_PAGE_1, _build_fetcher

    fetcher = _build_fetcher({"pg=1": _LIST_PAGE_1.encode(), "pg=2": _EMPTY_LIST_PAGE.encode()})
    ids = teac_seed.collect_criterio_ids(fd="01/01/2024", fh="31/12/2024", fetcher=fetcher)
    _, metadata = teac_seed.fetch_criterio(ids[0], tmp_path, fetcher=_build_fetcher({"criterio.aspx": _CRITERIO_VALID.encode()}))  # fmt: skip

    assert len(ids) == 3 and metadata is not None
    assert _estado("teac_listado") == "ok" and _ultima("teac_listado").metricas == {"criterios": 3.0}
    # The synthetic criterio has no text of the criterion: the watch says so.
    assert "criterio" in {f.canario for f in _ultima("teac_criterio").fallos}


def test_dgt_v1_reads_results_and_documents_like_the_old_code() -> None:
    busqueda, documento = util.leer("dgt_busqueda_sintetica.html"), util.leer("dgt_documento_sintetico.html")
    reglas = formatos.reglas_de("dgt_busqueda", dgt_seed.ReglasBusquedaDgt)

    for html in (busqueda, busqueda.replace("<strong>", "<b>"), "<html></html>"):
        nuevo = dgt_seed.extraer_busqueda(reglas, html)
        filas, paginas, query = legacy.dgt_resultados(html)
        assert ([tuple(f) for f in nuevo["filas"]], nuevo["total_paginas"] or 1, nuevo["query"]) == (filas, paginas, query)  # fmt: skip
    for html in (documento, documento.replace("V0058-25", "V0058-25 (anulada)"), "<p>nada</p>"):
        assert dgt_seed._parse_doc_num(html) == legacy.dgt_parse_doc_num(html)


def test_dgt_canaries() -> None:
    busqueda, documento = util.leer("dgt_busqueda_sintetica.html"), util.leer("dgt_documento_sintetico.html")

    assert _graves("dgt_busqueda", busqueda) == set()
    # A month without consultas is still a recognisable page.
    assert _graves("dgt_busqueda", busqueda.split("<table")[0] + busqueda.split("</table>")[1]) == set()
    assert _graves("dgt_busqueda", "<html><body>Servicio no disponible</body></html>") == {"pagina"}
    assert _graves("dgt_busqueda", busqueda.replace('id="query"', 'id="q"')) == {"query"}

    assert _graves("dgt_documento", documento, "101:V0058-25") == set()
    assert _graves("dgt_documento", documento, "102:V2007-05") == {"otro_documento"}
    assert _graves("dgt_documento", documento.replace('class="NUM-CONSULTA"', 'class="numero"'), "101:") == {"numero"}


def test_boe_seed_reports_what_it_saved(vigilancia, tmp_path, monkeypatch) -> None:
    cabecera = util.leer("boe_seed_BOE-A-2013-954_cabecera.html")
    respuestas = {"BOE-A-2013-954": cabecera, "BOE-A-2017-8345": "<html><title>Error 500</title></html>"}

    class Respuesta:
        def __init__(self, cuerpo: str) -> None:
            self.cuerpo = cuerpo

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return self.cuerpo.encode()

    monkeypatch.setattr(
        boe_seed.urllib.request, "urlopen", lambda req, **_: Respuesta(respuestas[req.full_url.rsplit("=", 1)[1]])
    )

    assert boe_seed.fetch_document("BOE-A-2013-954", tmp_path / "a.html") == len(cabecera.encode())
    assert _estado("boe_seed") == "ok"
    boe_seed.fetch_document("BOE-A-2017-8345", tmp_path / "b.html")
    assert _estado("boe_seed") == "degradada" and [f.canario for f in _ultima("boe_seed").fallos] == ["identificador"]
    assert (tmp_path / "b.html").read_text() == respuestas["BOE-A-2017-8345"]


# --------------------------------------------------------------------------- #
# Every new spec, and the LLM price table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "clave",
    ["cendoj_documento", "aeat_sede", "calendario_boe", "teac_listado", "dgt_busqueda", "dgt_documento"],
)
def test_new_specs_refuse_unknown_keys_and_unsafe_patterns(clave: str) -> None:
    fuente = formatos.fuente_de(clave)
    v1 = formatos.spec_incorporada(clave).reglas
    assert fuente.reglas is not None
    with pytest.raises(ValueError):
        fuente.reglas(**{**v1, "otra": "x"})
    patron = next((k for k in v1 if k.startswith("patron") or k == "rutas_permitidas"), None)
    if patron is not None:
        with pytest.raises(ValueError):
            fuente.reglas(**{**v1, patron: ["(a+)+$"] if isinstance(v1[patron], list) else "(a+)+$"})


def test_price_table_names_configured_models_without_a_price(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_MODEL", "claude-opus-5-5")
    for nombre in ("FORMAT_MODEL", "FORMAT_REVIEWER_MODEL", "RECODE_MODEL", "RECODE_REVIEWER_MODEL", "VERIFIER_MODEL"):
        monkeypatch.setattr(settings, nombre, "")
    assert cli_formatos.avisos_de_datos_propios(HOY) == []

    monkeypatch.setattr(settings, "FORMAT_REVIEWER_MODEL", "claude-otro-modelo")
    avisos = cli_formatos.avisos_de_datos_propios(HOY)
    assert len(avisos) == 1 and "claude-otro-modelo (FORMAT_REVIEWER_MODEL)" in avisos[0]

    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_AGENT_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "FORMAT_REVIEWER_MODEL", "")
    assert "gpt-4o-mini (OPENAI_AGENT_MODEL)" in cli_formatos.avisos_de_datos_propios(HOY)[0]
    # A table dated in the future is a typo worth saying.
    assert "fecha futura" in cli_formatos.avisos_de_datos_propios(date(2026, 1, 1))[0]
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    assert cli_formatos.avisos_de_datos_propios(HOY + timedelta(days=1)) == []
