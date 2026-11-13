"""Tests for the selective Tribunal Supremo ingestion (Sala Tercera, Sección Segunda).

All documents are synthetic fixtures modelled on the layout of CENDOJ
rulings; nothing here touches the network.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import supremo_seed
from hacienda_gpt.crawler.cendoj import parse_cendoj_document
from hacienda_gpt.doctrine.supremo import (
    MAX_PASSAGE_CHARS,
    SentenciaTS,
    analizar_documento,
    analizar_sentencia,
    calcular_prioridad,
    es_casacion_nueva,
    es_objetivo_prioritario,
    sidecar_metadata,
)

CUESTION = (
    "Determinar si los intereses de demora abonados por la Agencia Tributaria al efectuar una devolución de "
    "ingresos indebidos se encuentran sujetos y no exentos del IRPF, constituyendo una ganancia patrimonial."
)
DOCTRINA = (
    "los intereses de demora abonados por la Agencia Tributaria al efectuar una devolución de ingresos indebidos "
    "se encuentran sujetos y no exentos del impuesto sobre la renta, constituyendo una ganancia patrimonial que "
    "constituye renta general, de conformidad con lo dispuesto en el art. 46, letra b) de la LIRPF."
)

FJ_DOCTRINA = f"""
<p>TERCERO.- Contenido interpretativo de esta sentencia.</p>
<p>Con arreglo a lo que establece el artículo 93.1 LJCA, procede fijar la interpretación de las normas
sobre las que el auto de admisión consideró necesario el enjuiciamiento.</p>
<p>La respuesta a la cuestión, conforme a lo que hemos razonado, debe ser que {DOCTRINA}</p>
"""

FALLO_DOCTRINA = (
    "<p>1º) Fijar los criterios interpretativos expresados en el fundamento jurídico tercero de esta sentencia.</p>"
)


def _sentencia_html(
    *,
    roj: str = "STS 1234/2021",
    ecli: str = "ES:TS:2021:1234",
    id_cendoj: str | None = "28079130022021100123",
    organo: str = "Tribunal Supremo. Sala de lo Contencioso",
    seccion_cabecera: str | None = "2",
    seccion_titulo: str = "Sección Segunda",
    sala_titulo: str = "Sala de lo Contencioso-Administrativo",
    fecha: str = "25/03/2021",
    recurso: str = "1234/2019",
    resolucion: str = "456/2021",
    procedimiento: str = "Recurso de Casación Contencioso-Administrativo (L.O. 7/2015)",
    tipo_procedimiento: str = "R. CASACION",
    con_cuestion: bool = True,
    fundamento_materia: str | None = None,
    fundamento_doctrina: str = "",
    fallo_extra: str = "",
) -> str:
    """Build a synthetic ruling that follows the CENDOJ header + judgment layout."""
    id_line = f"<p>Id Cendoj: {id_cendoj}</p>" if id_cendoj else ""
    seccion_line = f"<p>Sección: {seccion_cabecera}</p>" if seccion_cabecera else ""
    cuestion = (
        "<p>SEGUNDO.- Admisión del recurso. La Sección Primera de la Sala Tercera del Tribunal Supremo admitió el "
        "recurso de casación mediante auto en el que aprecia un interés casacional objetivo para la formación de "
        "jurisprudencia, enunciado en estos literales términos:</p>"
        f"<p>«2º) La cuestión que presenta interés casacional objetivo para la formación de la jurisprudencia "
        f"consiste en: {CUESTION}</p>"
        "<p>3º) Identificar como normas jurídicas que, en principio, habrán de ser objeto de interpretación los "
        "artículos 33.1 y 46 de la Ley 35/2006.»</p>"
        if con_cuestion
        else "<p>SEGUNDO.- Tramitación. Se dio traslado a las partes, que formularon sus alegaciones.</p>"
    )
    materia = fundamento_materia or (
        "La controversia versa sobre la tributación en el IRPF de los intereses de demora satisfechos por la "
        "Agencia Tributaria (AEAT), a la luz de la Ley General Tributaria y de la liquidación tributaria girada "
        "al contribuyente."
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><title>Consejo General del Poder Judicial: Buscador de contenidos</title></head>
<body>
  <div class="cabecera">
    <p>Roj: {roj} - ECLI:{ecli}</p>
    {id_line}
    <p>Órgano: {organo}</p>
    <p>Sede: Madrid</p>
    {seccion_line}
    <p>Fecha: {fecha}</p>
    <p>Nº de Recurso: {recurso}</p>
    <p>Nº de Resolución: {resolucion}</p>
    <p>Procedimiento: {procedimiento}</p>
    <p>Ponente: JOSE ANTONIO MONTERO FERNANDEZ</p>
    <p>Tipo de Resolución: Sentencia</p>
  </div>
  <div class="contenido">
    <p>T R I B U N A L S U P R E M O</p>
    <p>{sala_titulo}</p>
    <p>{seccion_titulo}</p>
    <p>Sentencia núm. {resolucion}</p>
    <p>Fecha de sentencia: {fecha}</p>
    <p>Tipo de procedimiento: {tipo_procedimiento}</p>
    <p>Número del procedimiento: {recurso}</p>
    <p>ANTECEDENTES DE HECHO</p>
    <p>PRIMERO.- Resolución recurrida. El objeto del recurso lo constituye la sentencia dictada el
    3 de mayo de 2018 por el Tribunal Superior de Justicia de la Comunidad Valenciana.</p>
    {cuestion}
    <p>FUNDAMENTOS DE DERECHO</p>
    <p>PRIMERO.- Objeto del recurso.</p>
    <p>{materia}</p>
    <p>SEGUNDO.- El criterio de la Sala.</p>
    <p>La sentencia recurrida aplica la doctrina del Tribunal Constitucional (STC 59/2017) y la doctrina del
    Tribunal de Justicia de la Unión Europea sobre el principio de efectividad. Véase también la sentencia de
    3 de diciembre de 2020 (rec. 7763/2019).</p>
    {fundamento_doctrina}
    <p>CUARTO.- Costas.</p>
    <p>No procede hacer declaración expresa de condena en las costas del recurso.</p>
    <p>F A L L O</p>
    <p>Por todo lo expuesto, en nombre del Rey y por la autoridad que le confiere la Constitución, esta Sala
    ha decidido:</p>
    {fallo_extra}
    <p>2º) Declarar no haber lugar al recurso.</p>
    <p>Notifíquese esta resolución a las partes.</p>
  </div>
</body>
</html>
"""


@pytest.fixture
def html_doctrina() -> str:
    return _sentencia_html(fundamento_doctrina=FJ_DOCTRINA, fallo_extra=FALLO_DOCTRINA)


@pytest.fixture
def html_sin_doctrina() -> str:
    return _sentencia_html(roj="STS 2222/2021", ecli="ES:TS:2021:2222")


@pytest.fixture
def html_seccion_cuarta() -> str:
    return _sentencia_html(
        roj="STS 3333/2021",
        ecli="ES:TS:2021:3333",
        id_cendoj="28079130042021100077",
        seccion_cabecera="4",
        seccion_titulo="Sección Cuarta",
        fundamento_doctrina=FJ_DOCTRINA,
        fallo_extra=FALLO_DOCTRINA,
    )


@pytest.fixture
def html_sala_civil() -> str:
    return _sentencia_html(
        roj="STS 4444/2021",
        ecli="ES:TS:2021:4444",
        id_cendoj="28079110012021100099",
        organo="Tribunal Supremo. Sala de lo Civil",
        seccion_cabecera="1",
        seccion_titulo="Sección Primera",
        sala_titulo="Sala de lo Civil",
        procedimiento="Recurso de casación",
        tipo_procedimiento="CASACIÓN",
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def test_casacion_nueva_que_fija_doctrina(html_doctrina: str) -> None:
    s = analizar_sentencia(html_doctrina, source_url="https://www.poderjudicial.es/search/AN/openDocument/abc")

    assert s.roj == "STS 1234/2021"
    assert s.ecli == "ES:TS:2021:1234"
    assert s.fecha == date(2021, 3, 25)
    assert "Tercera" in (s.sala or "")
    assert s.seccion == 2
    assert s.tipo_recurso == "casacion"
    assert s.numero_recurso == "1234/2019"
    assert s.numero_sentencia == "456/2021"
    assert s.es_tributaria is True
    assert "IRPF" in s.tributos
    assert es_objetivo_prioritario(s)
    assert es_casacion_nueva(s)
    assert s.fija_doctrina is True
    assert s.prioridad == 100
    assert any("Fija doctrina" in motivo for motivo in s.motivos)
    assert any("Sección Segunda" in motivo for motivo in s.motivos)


def test_extrae_pasaje_de_doctrina_y_cuestion(html_doctrina: str) -> None:
    s = analizar_sentencia(html_doctrina)

    assert s.doctrina is not None
    assert s.doctrina.startswith("La respuesta a la cuestión")
    assert "se encuentran sujetos y no exentos del impuesto sobre la renta" in s.doctrina
    # The passage stops at the next ground and never reaches the FALLO.
    assert "Costas" not in s.doctrina
    assert "Declarar no haber lugar" not in s.doctrina

    assert s.cuestion_casacional is not None
    assert s.cuestion_casacional.startswith("Determinar si los intereses de demora")
    assert s.cuestion_casacional.endswith("ganancia patrimonial.")
    assert "Identificar como normas" not in s.cuestion_casacional


def test_doctrina_por_titulo_del_fundamento() -> None:
    fj = (
        "<p>TERCERO.- Criterios interpretativos sobre el artículo 46 de la LIRPF.</p>"
        "<p>Los intereses de demora abonados por la Agencia Tributaria están sujetos y no exentos del impuesto.</p>"
    )
    s = analizar_sentencia(_sentencia_html(fundamento_doctrina=fj))

    assert s.fija_doctrina is True
    assert (
        s.doctrina
        == "Los intereses de demora abonados por la Agencia Tributaria están sujetos y no exentos del impuesto."
    )
    assert s.prioridad == 100


def test_doctrina_solo_por_el_fallo_recupera_el_fundamento_citado() -> None:
    fj = "<p>TERCERO.- Resolución de las pretensiones.</p><p>Los intereses de demora tributan como ganancia.</p>"
    s = analizar_sentencia(_sentencia_html(fundamento_doctrina=fj, fallo_extra=FALLO_DOCTRINA))

    assert s.fija_doctrina is True
    assert s.doctrina is not None
    assert "Los intereses de demora tributan como ganancia." in s.doctrina
    assert "Costas" not in s.doctrina


def test_pasaje_de_doctrina_se_recorta_en_frontera_de_frase() -> None:
    frase = "El art. 46 de la ley somete a gravamen los intereses percibidos por el contribuyente en el ejercicio. "
    fj = f"<p>TERCERO.- Criterios interpretativos.</p><p>Fijamos la siguiente doctrina: {frase * 40}</p>"
    s = analizar_sentencia(_sentencia_html(fundamento_doctrina=fj))

    assert s.doctrina is not None
    assert 500 < len(s.doctrina) <= MAX_PASSAGE_CHARS
    assert s.doctrina.endswith("en el ejercicio.")


def test_casacion_nueva_sin_pasaje_de_doctrina(html_sin_doctrina: str) -> None:
    s = analizar_sentencia(html_sin_doctrina)

    assert es_objetivo_prioritario(s)
    assert es_casacion_nueva(s)
    assert s.fija_doctrina is False
    assert s.doctrina is None
    assert s.cuestion_casacional is not None
    assert s.prioridad == 70
    assert any("No se ha detectado un pasaje que fije doctrina" in motivo for motivo in s.motivos)


def test_citar_doctrina_de_otros_tribunales_no_es_fijar_doctrina() -> None:
    materia = (
        "La liquidación tributaria del IRPF se apoya en la doctrina del Tribunal Constitucional y en la doctrina "
        "reiterada de la Audiencia Nacional. La recurrente invoca además la doctrina de los actos propios y la "
        "doctrina administrativa del TEAC, que fijó como criterio la sujeción de los intereses."
    )
    s = analizar_sentencia(_sentencia_html(con_cuestion=False, fundamento_materia=materia))

    assert s.fija_doctrina is False
    assert s.doctrina is None
    assert s.cuestion_casacional is None
    assert s.prioridad == 70
    assert any("no menciona el interés casacional objetivo" in motivo for motivo in s.motivos)


def test_sentencia_anterior_a_la_reforma() -> None:
    html = _sentencia_html(
        roj="STS 900/2014",
        ecli="ES:TS:2014:900",
        id_cendoj="28079130022014100050",
        fecha="10/03/2014",
        recurso="2345/2011",
        resolucion="300/2014",
        procedimiento="Recurso de casación",
        con_cuestion=False,
    )
    s = analizar_sentencia(html)

    assert s.fecha == date(2014, 3, 10)
    assert es_objetivo_prioritario(s)
    assert s.tipo_recurso == "casacion"
    assert not es_casacion_nueva(s)
    assert s.prioridad == 40
    assert any("anterior al régimen de la LO 7/2015" in motivo for motivo in s.motivos)


@pytest.mark.parametrize(("fecha", "nueva"), [("21/07/2016", False), ("22/07/2016", True)])
def test_frontera_del_nuevo_regimen(fecha: str, nueva: bool) -> None:
    s = analizar_sentencia(_sentencia_html(fecha=fecha))
    assert es_casacion_nueva(s) is nueva


def test_unificacion_de_doctrina_no_es_casacion_del_nuevo_regimen() -> None:
    html = _sentencia_html(
        fecha="10/03/2017",
        procedimiento="Recurso de casación para la unificación de doctrina",
        tipo_procedimiento="R. CASACION UNIFICACION DOCTRINA",
        con_cuestion=False,
    )
    s = analizar_sentencia(html)

    assert s.tipo_recurso == "otro"
    assert not es_casacion_nueva(s)
    assert es_objetivo_prioritario(s)
    assert s.prioridad == 30


def test_otra_seccion_de_la_sala_tercera_no_es_objetivo(html_seccion_cuarta: str) -> None:
    s = analizar_sentencia(html_seccion_cuarta)

    assert "Tercera" in (s.sala or "")
    assert s.seccion == 4
    assert not es_objetivo_prioritario(s)
    assert s.prioridad == 0
    assert any("Sección 4" in motivo for motivo in s.motivos)


def test_otra_sala_no_es_objetivo(html_sala_civil: str) -> None:
    # The body cites the "Sala Tercera" (admission paragraph), which is what
    # the base parser latches on; the header must win.
    assert "Sala Tercera" in html_sala_civil
    s = analizar_sentencia(html_sala_civil)

    assert s.sala == "Sala Primera (Civil)"
    assert not es_objetivo_prioritario(s)
    assert s.prioridad == 0
    assert any("No es de la Sala Tercera" in motivo for motivo in s.motivos)


def test_otra_sala_sin_id_cendoj() -> None:
    html = _sentencia_html(
        id_cendoj=None,
        organo="Tribunal Supremo. Sala de lo Social",
        seccion_cabecera="1",
        seccion_titulo="Sección Primera",
        sala_titulo="Sala de lo Social",
    )
    s = analizar_sentencia(html)

    assert s.sala == "Sala Cuarta (Social)"
    assert s.prioridad == 0


def test_seccion_indeterminada_no_cualifica() -> None:
    html = _sentencia_html(id_cendoj=None, seccion_cabecera=None, seccion_titulo="Sentencia")
    s = analizar_sentencia(html)

    assert s.seccion is None
    assert not es_objetivo_prioritario(s)
    assert s.prioridad == 0
    assert any("No se ha podido determinar la sección" in motivo for motivo in s.motivos)


@pytest.mark.parametrize(
    "titulo",
    ["Sección Segunda", "Sección 2ª", "Sección 2.ª", "SECCIÓN SEGUNDA", "Sección: 2"],
)
def test_formas_de_la_seccion_segunda(titulo: str) -> None:
    html = _sentencia_html(id_cendoj=None, seccion_cabecera=None, seccion_titulo=titulo)
    assert analizar_sentencia(html).seccion == 2


def test_la_seccion_de_admision_citada_en_el_cuerpo_no_cuenta() -> None:
    # "Sección Primera" appears in the admission paragraph, far from the header.
    relleno = "<p>" + "Texto de relleno de la cabecera. " * 80 + "</p>"
    html = _sentencia_html(id_cendoj=None, seccion_cabecera=None, seccion_titulo="Sentencia").replace(
        "<p>ANTECEDENTES DE HECHO</p>", relleno + "<p>ANTECEDENTES DE HECHO</p>"
    )
    assert analizar_sentencia(html).seccion is None


def test_no_tributaria_no_es_objetivo() -> None:
    html = """<html><body>
    <p>Roj: STS 5555/2021 - ECLI:ES:TS:2021:5555</p>
    <p>Órgano: Tribunal Supremo. Sala de lo Contencioso</p>
    <p>Sección: 2</p>
    <p>Fecha: 25/03/2021</p>
    <p>Procedimiento: Recurso de Casación Contencioso-Administrativo (L.O. 7/2015)</p>
    <p>FUNDAMENTOS DE DERECHO</p>
    <p>PRIMERO.- Se discute la denegación de una licencia urbanística de obras por el Ayuntamiento.</p>
    </body></html>"""
    s = analizar_sentencia(html)

    assert s.seccion == 2
    assert s.es_tributaria is False
    assert s.prioridad == 0
    assert any("materia tributaria" in motivo for motivo in s.motivos)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Recurso de casación núm. 1234/2019", "1234/2019"),
        ("recurso de casación n.º 1234/2019", "1234/2019"),
        ("recurso de casación 1.234/2019", "1234/2019"),
        ("sentencia recaída en el rec. 1234/2019", "1234/2019"),
        ("RCA/1234/2019", "1234/2019"),
        ("RCA 987/2020", "987/2020"),
        ("R. CASACION núm.: 1234/2019", "1234/2019"),
        ("R. CASACION núm.: 0056/2018", "56/2018"),
        ("Nº de Recurso: 6950/2018", "6950/2018"),
        ("Número del procedimiento: 6950/2018", "6950/2018"),
    ],
)
def test_normalizacion_del_numero_de_recurso(texto: str, esperado: str) -> None:
    html = f"<html><body><p>Tribunal Supremo. Sala Tercera. Sección Segunda.</p><p>{texto}</p></body></html>"
    assert analizar_sentencia(html).numero_recurso == esperado


def test_el_numero_de_recurso_propio_prevalece_sobre_las_citas() -> None:
    html = (
        "<html><body><p>Sala Tercera. Sección Segunda.</p>"
        "<p>Como dijimos en la sentencia de 3 de diciembre de 2020 (rec. 7763/2019), la cuestión es pacífica.</p>"
        "<p>R. CASACION núm.: 1234/2019</p></body></html>"
    )
    assert analizar_sentencia(html).numero_recurso == "1234/2019"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Sentencia núm. 1.456/2021", "1456/2021"),
        ("SENTENCIA n.º 88/2022", "88/2022"),
        ("Nº de Resolución: 456/2021", "456/2021"),
        ("STS núm. 1456/2021", "1456/2021"),
    ],
)
def test_normalizacion_del_numero_de_sentencia(texto: str, esperado: str) -> None:
    html = f"<html><body><p>Sala Tercera. Sección Segunda.</p><p>{texto}</p></body></html>"
    assert analizar_sentencia(html).numero_sentencia == esperado


def test_documento_vacio_no_falla() -> None:
    s = analizar_sentencia("<html><body></body></html>")

    assert s.roj is None
    assert s.seccion is None
    assert s.prioridad == 0
    assert s.motivos


def test_prioridad_es_recalculable_y_el_modelo_es_estricto(html_doctrina: str) -> None:
    s = analizar_sentencia(html_doctrina)
    assert calcular_prioridad(s) == s.prioridad
    with pytest.raises(ValueError, match="campo_desconocido"):
        SentenciaTS.model_validate({"campo_desconocido": 1})


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_sidecar_metadata(html_doctrina: str) -> None:
    url = "https://www.poderjudicial.es/search/AN/openDocument/abc"
    s, base = analizar_documento(html_doctrina, source_url=url)
    original = dict(base)

    sidecar = sidecar_metadata(s, base)

    assert base == original, "base must not be mutated"
    assert set(base) <= set(sidecar)
    assert sidecar["source_authority"] == parse_cendoj_document(html_doctrina)["source_authority"] == "TS_SALA_TERCERA"
    assert sidecar["source_url"] == url
    assert sidecar["roj"] == "STS 1234/2021"
    assert sidecar["fecha_resolucion"] == "2021-03-25"
    assert sidecar["seccion"] == 2
    assert sidecar["tipo_recurso"] == "casacion"
    assert sidecar["numero_recurso"] == "1234/2019"
    assert sidecar["numero_sentencia"] == "456/2021"
    assert sidecar["fija_doctrina"] is True
    assert sidecar["doctrina_fijada"] == s.doctrina
    assert sidecar["cuestion_casacional"] == s.cuestion_casacional
    assert sidecar["prioridad_doctrinal"] == 100
    assert sidecar["document_type"] == "sentencia_ts"
    # JSON round-trip: everything is serialisable as is.
    assert json.loads(json.dumps(sidecar)) == sidecar


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def sin_red(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make any real download fail loudly and record the pauses instead of sleeping."""

    def _prohibido(url: str) -> bytes:
        raise AssertionError(f"unexpected network access: {url}")

    pausas: list[float] = []
    monkeypatch.setattr(supremo_seed, "_http_get", _prohibido)
    monkeypatch.setattr(supremo_seed, "_pausa", pausas.append)
    monkeypatch.setattr(supremo_seed.requests, "get", lambda *args, **kwargs: _prohibido(str(args)))
    return pausas


@pytest.fixture
def directorio_html(
    tmp_path: Path, html_doctrina: str, html_sin_doctrina: str, html_seccion_cuarta: str, html_sala_civil: str
) -> Path:
    origen = tmp_path / "descargas"
    (origen / "sub").mkdir(parents=True)
    (origen / "a.html").write_text(html_doctrina, encoding="utf-8")
    (origen / "b.html").write_text(html_sin_doctrina, encoding="utf-8")
    (origen / "sub" / "c.html").write_text(html_seccion_cuarta, encoding="utf-8")
    (origen / "d.html").write_text(html_sala_civil, encoding="utf-8")
    (origen / "notas.txt").write_text("no es html", encoding="utf-8")
    return origen


def test_cli_from_dir_escribe_conservadas_y_manifiesto(
    directorio_html: Path, tmp_path: Path, sin_red: list[float]
) -> None:
    salida = tmp_path / "salida"
    result = CliRunner().invoke(supremo_seed.cli, ["--from-dir", str(directorio_html), "--output-dir", str(salida)])

    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in salida.iterdir()) == [
        "STS_1234_2021.html",
        "STS_1234_2021.json",
        "STS_2222_2021.html",
        "STS_2222_2021.json",
        "supremo-seed-manifest.json",
    ]
    assert (salida / "STS_1234_2021.html").read_bytes() == (directorio_html / "a.html").read_bytes()
    sidecar = json.loads((salida / "STS_1234_2021.json").read_text(encoding="utf-8"))
    assert sidecar["document_type"] == "sentencia_ts"
    assert sidecar["prioridad_doctrinal"] == 100
    assert sidecar["doctrina_fijada"].startswith("La respuesta a la cuestión")

    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest["totales"] == {
        "analizadas": 4,
        "conservadas": 2,
        "descartadas": 2,
        "fijan_doctrina": 1,
        "errores_descarga": 0,
        "urls_rechazadas": 0,
        "urls_omitidas": 0,
    }
    por_slug = {doc["slug"]: doc for doc in manifest["documentos"]}
    assert set(por_slug) == {"STS_1234_2021", "STS_2222_2021", "STS_3333_2021", "STS_4444_2021"}
    conservada = por_slug["STS_1234_2021"]
    assert {"origen", "slug", "roj", "fecha", "prioridad", "conservada", "motivos"} <= set(conservada)
    assert conservada["conservada"] is True
    assert conservada["roj"] == "STS 1234/2021"
    assert conservada["fecha"] == "2021-03-25"
    assert conservada["prioridad"] == 100
    assert conservada["origen"].endswith("a.html")
    assert por_slug["STS_3333_2021"]["conservada"] is False
    assert por_slug["STS_3333_2021"]["motivos"]
    assert por_slug["STS_4444_2021"]["conservada"] is False

    assert "2 conservadas (1 fijan doctrina), 2 descartadas" in result.output
    # No network mode involved: the CENDOJ notice is only for --urls-file.
    assert "AVISO" not in result.output
    assert sin_red == []


def test_cli_min_priority_descarta_las_de_menor_prioridad(
    directorio_html: Path, tmp_path: Path, sin_red: list[float]
) -> None:
    salida = tmp_path / "salida"
    result = CliRunner().invoke(
        supremo_seed.cli,
        ["--from-dir", str(directorio_html), "--output-dir", str(salida), "--min-priority", "80"],
    )

    assert result.exit_code == 0, result.output
    assert (salida / "STS_1234_2021.html").exists()
    assert not (salida / "STS_2222_2021.html").exists()
    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    descartada = next(doc for doc in manifest["documentos"] if doc["slug"] == "STS_2222_2021")
    assert descartada["conservada"] is False
    assert any("inferior al mínimo" in motivo for motivo in descartada["motivos"])


def test_cli_dry_run_no_escribe_nada(directorio_html: Path, tmp_path: Path, sin_red: list[float]) -> None:
    salida = tmp_path / "salida"
    result = CliRunner().invoke(
        supremo_seed.cli, ["--from-dir", str(directorio_html), "--output-dir", str(salida), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not salida.exists()
    assert "2 conservadas (1 fijan doctrina), 2 descartadas" in result.output
    assert "No se ha escrito ningún fichero" in result.output


def test_cli_no_reprocesa_su_propia_salida(directorio_html: Path, sin_red: list[float]) -> None:
    salida = directorio_html / "seleccion"
    args = ["--from-dir", str(directorio_html), "--output-dir", str(salida)]
    assert CliRunner().invoke(supremo_seed.cli, args).exit_code == 0
    result = CliRunner().invoke(supremo_seed.cli, args)

    assert result.exit_code == 0, result.output
    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest["totales"]["analizadas"] == 4
    assert manifest["totales"]["conservadas"] == 2


def test_cli_slug_de_respaldo_y_duplicados(tmp_path: Path, sin_red: list[float]) -> None:
    sin_identificador = _sentencia_html().replace("<p>Roj: STS 1234/2021 - ECLI:ES:TS:2021:1234</p>", "")
    origen = tmp_path / "descargas"
    origen.mkdir()
    (origen / "x.html").write_text(sin_identificador, encoding="utf-8")
    (origen / "y.html").write_text(sin_identificador, encoding="utf-8")
    salida = tmp_path / "salida"

    result = CliRunner().invoke(supremo_seed.cli, ["--from-dir", str(origen), "--output-dir", str(salida)])

    assert result.exit_code == 0, result.output
    htmls = sorted(path.name for path in salida.glob("*.html"))
    assert len(htmls) == 1
    assert htmls[0].startswith("doc_")
    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert [doc["conservada"] for doc in manifest["documentos"]] == [True, False]
    assert any("duplicada" in motivo for motivo in manifest["documentos"][1]["motivos"])


def test_cli_exige_alguna_fuente(tmp_path: Path) -> None:
    result = CliRunner().invoke(supremo_seed.cli, ["--output-dir", str(tmp_path / "salida")])

    assert result.exit_code != 0
    assert "--from-dir o --urls-file" in result.output


def test_cli_urls_descarga_una_a_una_y_rechaza_otros_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html_doctrina: str, html_seccion_cuarta: str, sin_red: list[float]
) -> None:
    buena = "https://www.poderjudicial.es/search/AN/openDocument/aaa111/20210405"
    otra = "https://poderjudicial.es/search/AN/openDocument/bbb222/20210405"
    paginas = {buena: html_doctrina, otra: html_seccion_cuarta}
    pedidas: list[str] = []

    def _fake_get(url: str) -> bytes:
        pedidas.append(url)
        return paginas[url].encode("utf-8")

    monkeypatch.setattr(supremo_seed, "_http_get", _fake_get)
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "\n".join(
            [
                "# sentencias seleccionadas a mano",
                "",
                buena,
                "https://www.boe.es/buscar/doc.php?id=BOE-A-2003-23186",
                "https://www.poderjudicial.es.evil.example/search/doc",
                "http://www.poderjudicial.es/search/AN/openDocument/ccc333",
                "ftp://poderjudicial.es/doc",
                otra,
                buena,
            ]
        ),
        encoding="utf-8",
    )
    salida = tmp_path / "salida"

    result = CliRunner().invoke(supremo_seed.cli, ["--urls-file", str(urls_file), "--output-dir", str(salida)])

    assert result.exit_code == 0, result.output
    assert pedidas == [buena, otra]
    assert sin_red == [5.0], "one pause between the two downloads, default delay"
    assert "AVISO" in result.output
    assert "CGPJ" in result.output
    assert "consulta individual" in result.output

    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest["totales"]["urls_rechazadas"] == 4
    assert all(
        "poderjudicial.es/search/AN/openDocument" not in url or url.startswith("http://")
        for url in manifest["urls_rechazadas"]
    )
    assert [doc["origen"] for doc in manifest["documentos"]] == [buena, otra]
    assert [doc["conservada"] for doc in manifest["documentos"]] == [True, False]
    sidecar = json.loads((salida / "STS_1234_2021.json").read_text(encoding="utf-8"))
    assert sidecar["source_url"] == buena
    assert not (salida / "STS_3333_2021.html").exists()


def test_cli_respeta_max_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html_doctrina: str, sin_red: list[float]
) -> None:
    urls = [f"https://www.poderjudicial.es/search/AN/openDocument/doc{i}" for i in range(6)]
    pedidas: list[str] = []

    def _fake_get(url: str) -> bytes:
        pedidas.append(url)
        return html_doctrina.encode("utf-8")

    monkeypatch.setattr(supremo_seed, "_http_get", _fake_get)
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("\n".join(urls), encoding="utf-8")
    salida = tmp_path / "salida"

    result = CliRunner().invoke(
        supremo_seed.cli,
        ["--urls-file", str(urls_file), "--output-dir", str(salida), "--max-downloads", "2", "--delay", "7"],
    )

    assert result.exit_code == 0, result.output
    assert pedidas == urls[:2]
    assert sin_red == [7.0]
    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest["urls_omitidas"] == urls[2:]
    assert manifest["totales"]["urls_omitidas"] == 4
    assert "se omiten" in result.output


def test_cli_max_downloads_por_defecto_es_25(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html_sala_civil: str, sin_red: list[float]
) -> None:
    pedidas: list[str] = []

    def _fake_get(url: str) -> bytes:
        pedidas.append(url)
        return html_sala_civil.encode("utf-8")

    monkeypatch.setattr(supremo_seed, "_http_get", _fake_get)
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "\n".join(f"https://www.poderjudicial.es/search/AN/openDocument/doc{i}" for i in range(30)), encoding="utf-8"
    )

    result = CliRunner().invoke(
        supremo_seed.cli, ["--urls-file", str(urls_file), "--output-dir", str(tmp_path / "salida"), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert len(pedidas) == 25
    assert not (tmp_path / "salida").exists()


@pytest.mark.parametrize("valor", ["101", "5000"])
def test_cli_rechaza_max_downloads_superior_a_100(tmp_path: Path, valor: str, sin_red: list[float]) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("https://www.poderjudicial.es/search/AN/openDocument/doc1\n", encoding="utf-8")
    salida = tmp_path / "salida"

    result = CliRunner().invoke(
        supremo_seed.cli, ["--urls-file", str(urls_file), "--output-dir", str(salida), "--max-downloads", valor]
    )

    assert result.exit_code != 0
    assert "descarga masiva" in result.output
    assert "CGPJ" in result.output
    assert not salida.exists()


def test_cli_rechaza_delay_inferior_a_un_segundo(tmp_path: Path, sin_red: list[float]) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("https://www.poderjudicial.es/search/AN/openDocument/doc1\n", encoding="utf-8")

    result = CliRunner().invoke(
        supremo_seed.cli,
        ["--urls-file", str(urls_file), "--output-dir", str(tmp_path / "salida"), "--delay", "0"],
    )

    assert result.exit_code != 0


def test_cli_error_de_descarga_y_bloqueo_del_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html_doctrina: str, sin_red: list[float]
) -> None:
    urls = [f"https://www.poderjudicial.es/search/AN/openDocument/doc{i}" for i in range(4)]
    pedidas: list[str] = []

    def _fake_get(url: str) -> bytes:
        pedidas.append(url)
        if url == urls[0]:
            raise supremo_seed.DescargaError("respuesta HTTP 500")
        if url == urls[2]:
            raise supremo_seed.DescargaError("respuesta HTTP 429", bloqueo=True)
        return html_doctrina.encode("utf-8")

    monkeypatch.setattr(supremo_seed, "_http_get", _fake_get)
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("\n".join(urls), encoding="utf-8")
    salida = tmp_path / "salida"

    result = CliRunner().invoke(supremo_seed.cli, ["--urls-file", str(urls_file), "--output-dir", str(salida)])

    assert result.exit_code == 0, result.output
    # A plain failure does not stop the run; a 403/429 does.
    assert pedidas == urls[:3]
    manifest = json.loads((salida / "supremo-seed-manifest.json").read_text(encoding="utf-8"))
    assert manifest["totales"]["errores_descarga"] == 2
    assert manifest["totales"]["conservadas"] == 1
    assert manifest["urls_omitidas"] == [urls[3]]
    assert "Error de descarga" in manifest["documentos"][0]["motivos"][0]


def test_http_get_usa_user_agent_generico_y_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[dict[str, object]] = []

    class _Respuesta:
        status_code = 200
        content = b"<html></html>"

    def _fake_requests_get(url: str, **kwargs: object) -> _Respuesta:
        llamadas.append({"url": url, **kwargs})
        return _Respuesta()

    monkeypatch.setattr(supremo_seed.requests, "get", _fake_requests_get)

    assert supremo_seed._http_get("https://www.poderjudicial.es/search/doc") == b"<html></html>"
    assert llamadas[0]["timeout"] == 30
    headers = llamadas[0]["headers"]
    assert isinstance(headers, dict)
    assert "hacienda-asesor" in headers["User-Agent"]
    assert "@" not in headers["User-Agent"]


def test_http_get_convierte_los_estados_de_bloqueo(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Respuesta:
        status_code = 429
        content = b""

    monkeypatch.setattr(supremo_seed.requests, "get", lambda url, **kwargs: _Respuesta())

    with pytest.raises(supremo_seed.DescargaError) as excinfo:
        supremo_seed._http_get("https://www.poderjudicial.es/search/doc")
    assert excinfo.value.bloqueo is True


@pytest.mark.parametrize(
    ("url", "permitida"),
    [
        ("https://www.poderjudicial.es/search/AN/openDocument/abc/20210405", True),
        ("https://poderjudicial.es/search/AN/openDocument/abc", True),
        ("http://www.poderjudicial.es/search/AN/openDocument/abc", False),
        ("https://www.poderjudicial.es.evil.example/doc", False),
        ("https://evil.example/?u=www.poderjudicial.es", False),
        ("https://www.poderjudicial.es@evil.example/doc", False),
        ("https://cendoj.example/doc", False),
        ("no es una url", False),
    ],
)
def test_es_url_permitida(url: str, permitida: bool) -> None:
    assert supremo_seed.es_url_permitida(url) is permitida
