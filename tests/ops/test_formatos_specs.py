"""Spec v1 of every source reproduces what the parser did before its format knowledge became data."""

from __future__ import annotations

from datetime import date
import json

import formatos_util as util
import legacy_parsers as legacy
import pytest

from hacienda_gpt import settings
from hacienda_gpt.cli import boe_watch
from hacienda_gpt.crawler import boletin_autonomico, teac
from hacienda_gpt.ops import formatos
from hacienda_gpt.ops.formatos_boe import ReglasConsolidado, ReglasMetadatos, ReglasSumario
from hacienda_gpt.proactive import impacto, novedades
from hacienda_gpt.tax import documentos, vigencia_normas

XML_REAL = "boe_documento_BOE-A-2023-26452.xml"
HTML_REAL = "boe_consolidado_BOE-A-2006-20764_recorte.html"

XML_CONVALIDADO = """<documento><metadatos><identificador>BOE-A-2099-1</identificador><rango>Real Decreto-ley</rango>
<estatus_derogacion>N</estatus_derogacion><fecha_derogacion/></metadatos><analisis><referencias><posteriores>
<posterior referencia="BOE-A-2099-2"><palabra codigo="1">Acuerdo de   convalidación</palabra>
<texto>por Resolución de 20 de enero</texto></posterior></posteriores></referencias></analisis></documento>"""
XML_DEROGADO_POR_REFERENCIA = XML_CONVALIDADO.replace("Acuerdo de   convalidación", "SE DEROGA")
XML_DEROGADO_POR_ESTATUS = (
    "<documento><metadatos><identificador>BOE-A-2099-1</identificador>"
    "<estatus_derogacion>S</estatus_derogacion><fecha_derogacion>20990301</fecha_derogacion></metadatos></documento>"
)
XML_PENDIENTE = "<documento><metadatos><identificador>BOE-A-2099-1</identificador></metadatos></documento>"


@pytest.mark.parametrize("fixture, dia", [("boe_sumario_20261110.json", date(2026, 11, 10)), ("boe_sumario_20261112.json", date(2026, 11, 12))])  # fmt: skip
def test_sumario_v1_reads_a_real_summary_like_the_old_parser(fixture: str, dia: date) -> None:
    payload = json.loads(util.leer(fixture))

    nuevos = novedades.items_del_sumario(payload, dia)

    assert nuevos == legacy.items_del_sumario(payload, dia)
    assert len(nuevos) >= 2 and any(item.seccion == "1" for item in nuevos)


def test_sumario_v1_matches_the_old_parser_on_odd_shapes() -> None:
    # A single object where a list is usual, an ``epigrafe`` nested in ``texto``, items without a title.
    payload = {
        "data": {
            "sumario": {
                "diario": {
                    "seccion": [
                        {
                            "codigo": "1",
                            "departamento": {
                                "nombre": "JEFATURA",
                                "item": {"identificador": "BOE-A-1", "titulo": "  Ley   1 ", "url_html": None},
                                "texto": {"epigrafe": [{"nombre": "Tributos", "item": [{"identificador": "BOE-A-2", "titulo": "x"}, {"identificador": "BOE-A-3"}]}]},
                            },
                        },
                        {"codigo": "5A", "departamento": [{"nombre": "X", "item": {"identificador": "BOE-B-9", "titulo": "y"}}]},
                        {"codigo": "3", "departamento": [None, {"nombre": "Y", "epigrafe": {"item": {"identificador": "BOE-A-4", "titulo": "z"}}}]},
                    ]
                }
            }
        }
    }  # fmt: skip

    assert novedades.items_del_sumario(payload, date(2026, 1, 2)) == legacy.items_del_sumario(payload, date(2026, 1, 2))
    assert novedades.items_del_sumario({}, date(2026, 1, 2)) == legacy.items_del_sumario({}, date(2026, 1, 2)) == []


@pytest.mark.parametrize("xml", [None, XML_CONVALIDADO, XML_PENDIENTE, "<no cierra", ""])
def test_analisis_v1_reads_the_xml_like_the_old_parser(xml: str | None) -> None:
    xml = util.leer(XML_REAL) if xml is None else xml
    vigiladas = frozenset({"BOE-A-2006-20764"})

    assert novedades.leer_analisis(xml, vigiladas) == legacy.leer_analisis(xml, vigiladas)


def test_analisis_of_the_real_fixture_is_not_empty() -> None:
    analisis = novedades.leer_analisis(util.leer(XML_REAL))

    assert analisis.rango == "Real Decreto-ley" and analisis.materias and analisis.modifica


@pytest.mark.parametrize(
    "xml, estado",
    [
        (XML_CONVALIDADO, "convalidada"),
        (XML_DEROGADO_POR_REFERENCIA, "derogada"),
        (XML_DEROGADO_POR_ESTATUS, "derogada"),
        (XML_PENDIENTE, "pendiente"),
        (None, "pendiente"),
    ],
)
def test_estado_v1_reads_validation_and_repeal_like_the_old_parser(xml: str | None, estado: str) -> None:
    xml = util.leer(XML_REAL) if xml is None else xml
    hoy = date(2026, 11, 12)

    nuevo = vigencia_normas.estado_desde_xml("BOE-A-2099-1", xml, hoy=hoy)

    assert nuevo == legacy.estado_desde_xml("BOE-A-2099-1", xml, hoy=hoy)
    assert nuevo.estado == estado


@pytest.mark.parametrize("desde", [date(1900, 1, 1), date(2009, 12, 18), date(2020, 1, 1), date(2100, 1, 1)])
def test_consolidado_v1_finds_the_same_changed_articles_as_the_old_parser(desde: date) -> None:
    html = util.leer(HTML_REAL)

    assert impacto.articulos_modificados(html, desde=desde) == legacy.articulos_modificados(html, desde=desde)


def test_consolidado_v1_matches_the_old_parser_on_the_existing_test_page() -> None:
    from tests.proactive.test_impacto import LIRPF_HTML

    nuevos = impacto.articulos_modificados(LIRPF_HTML, desde=date(2000, 1, 1))

    assert nuevos and nuevos == legacy.articulos_modificados(LIRPF_HTML, desde=date(2000, 1, 1))


def test_consolidado_v1_patterns_are_the_old_expressions() -> None:
    reglas = formatos.reglas_de("boe_consolidado_html", ReglasConsolidado)

    assert reglas.encabezado == "(?i)" + legacy._HEADING_RE.pattern
    assert reglas.frontera == legacy._BOUNDARY_RE.pattern
    assert reglas.version == "(?i)" + legacy._VERSION_RE.pattern
    assert reglas.nota == "(?i)" + legacy._NOTA_RE.pattern
    assert reglas.interfaz == "(?i)" + legacy._UI_RE.pattern


@pytest.mark.parametrize("fixture", ["criterio_dl.html", "criterio_tabla.html", "criterio_not_found.html"])
def test_teac_v1_reads_the_fixtures_like_the_old_parser(fixture: str) -> None:
    html = (util.FIXTURES / "teac" / fixture).read_text(encoding="utf-8")

    nuevo, viejo = teac.parse_teac_criterio(html, criterio_id=7), legacy.parse_teac_criterio(html, criterio_id=7)

    if viejo is None:
        assert nuevo is None
    else:
        assert nuevo is not None and list(nuevo) == list(viejo)
        assert {k: v for k, v in nuevo.items() if k != "ingested_at"} == {k: v for k, v in viejo.items() if k != "ingested_at"}  # fmt: skip


def test_boletin_v1_reads_the_listing_like_the_old_parser() -> None:
    html = (util.FIXTURES / "boletines" / "bocm_listing.html").read_text(encoding="utf-8")
    for spec in boletin_autonomico.BUNDLED_SPECS.values():
        assert boletin_autonomico.spec_vigente(spec) == spec
        assert boletin_autonomico.parse_listing(spec, html) == legacy.parse_listing(spec, html)
    assert len(boletin_autonomico.parse_listing(boletin_autonomico.BOCM_MADRID, html)) == 2


def test_metadatos_v1_returns_the_same_entry(monkeypatch) -> None:
    crudo = util.leer("boe_metadatos_BOE-A-2006-20764.json")

    class Respuesta:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return crudo.encode("utf-8")

    monkeypatch.setattr(boe_watch.urllib.request, "urlopen", lambda *_a, **_k: Respuesta())

    entrada = boe_watch._fetch_metadata("BOE-A-2006-20764")

    assert entrada == json.loads(crudo)["data"][0]
    assert entrada["fecha_actualizacion"] == "20261111T094003Z"


def test_documentos_v1_leaves_the_prompt_untouched() -> None:
    assert documentos.anexo_vocabulario() == ""


def test_every_bundled_spec_is_version_1_and_validates() -> None:
    for clave, fuente in formatos.fuentes().items():
        if fuente.reglas is None:
            continue
        spec = formatos.spec_incorporada(clave)
        assert (spec.fuente, spec.version, spec.origen) == (clave, 1, "incorporada")
        fuente.reglas(**spec.reglas)


def test_an_applied_spec_changes_what_the_parser_reads_and_a_broken_one_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "FORMAT_DIR", str(tmp_path))
    payload = {"status": {"code": "200"}, "data": [{"identificador": "BOE-A-1", "actualizado": "20260101T000000Z"}]}
    reglas = formatos.reglas_de("boe_metadatos", ReglasMetadatos)
    v1 = formatos.spec_incorporada("boe_metadatos")
    ruta = tmp_path / "specs" / "boe_metadatos.json"
    ruta.parent.mkdir(parents=True)

    from hacienda_gpt.ops.formatos_boe import entrada_metadatos

    assert "fecha_actualizacion" not in entrada_metadatos(reglas, payload)

    nuevas = {**v1.reglas, "fecha_actualizacion": ["fecha_actualizacion", "actualizado"]}
    ruta.write_text(json.dumps({"fuente": "boe_metadatos", "version": 2, "origen": "prueba", "reglas": nuevas}))
    reglas = formatos.reglas_de("boe_metadatos", ReglasMetadatos)
    assert entrada_metadatos(reglas, payload)["fecha_actualizacion"] == "20260101T000000Z"

    # Not valid JSON, rules of another shape, a spec for another source: the bundled one is used, never a crash.
    for roto in ("{no es json", json.dumps({"fuente": "boe_metadatos", "version": 3, "reglas": {"x": 1}}), json.dumps({"fuente": "otra", "version": 9, "reglas": nuevas})):  # fmt: skip
        ruta.write_text(roto)
        assert formatos.reglas_de("boe_metadatos", ReglasMetadatos) == ReglasMetadatos(**v1.reglas)


def test_alias_lets_one_spec_read_the_old_and_the_new_name() -> None:
    v1 = formatos.spec_incorporada("boe_sumario").reglas
    reglas = ReglasSumario(**{**v1, "identificador": ["identificador", "id_disposicion"]})
    viejo = json.loads(util.leer("boe_sumario_20261110.json"))
    nuevo = json.loads(util.leer("boe_sumario_20261110.json").replace('"identificador"', '"id_disposicion"'))

    assert novedades.extraer_items(reglas, viejo) == novedades.extraer_items(reglas, nuevo) != []
    assert novedades.extraer_items(ReglasSumario(**v1), nuevo) == []


@pytest.mark.parametrize("patron", ["(a+)+$", r"(\w*)*x", r"(a)\1", "(", "x" * 700, ""])
def test_unsafe_patterns_are_refused(patron: str) -> None:
    with pytest.raises(ValueError):
        formatos.patron_seguro(patron)


def test_rules_with_unknown_keys_or_bad_patterns_do_not_validate() -> None:
    v1 = formatos.spec_incorporada("boe_consolidado_html").reglas
    with pytest.raises(ValueError):
        ReglasConsolidado(**{**v1, "otra": "x"})
    with pytest.raises(ValueError):
        ReglasConsolidado(**{**v1, "version": r"publicado el (\d+)"})
    with pytest.raises(ValueError):
        ReglasConsolidado(**{**v1, "encabezado": "(a+)+"})
