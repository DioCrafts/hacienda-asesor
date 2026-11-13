"""Finding new tax norms in the BOE's daily issue (no LLM, no network in tests)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from click.testing import CliRunner

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.proactive.novedades import (
    ItemSumario,
    clasificar,
    items_del_sumario,
    leer_analisis,
    notificar_novedad,
    revisar,
)
from hacienda_gpt.proactive.store import NotificationStore
from tests.proactive import make_case

DIA = date(2026, 10, 7)
LIRPF = "BOE-A-2006-20764"
LEY_CLM = "BOE-A-2014-1368"


def _sumario(*items: tuple[str, str, str, str, str]) -> dict:
    """(seccion, departamento, epigrafe, id, titulo) → the JSON the BOE returns."""
    secciones: dict[str, dict] = {}
    for seccion, departamento, epigrafe, boe_id, titulo in items:
        sec = secciones.setdefault(seccion, {"codigo": seccion, "departamento": []})
        sec["departamento"].append(
            {
                "nombre": departamento,
                # One item comes as a dict, not a list: the API does that.
                "epigrafe": {
                    "nombre": epigrafe,
                    "item": {"identificador": boe_id, "titulo": titulo, "url_html": f"https://boe/{boe_id}"},
                },
            }
        )
    return {"data": {"sumario": {"diario": [{"seccion": list(secciones.values())}]}}}


def _xml(rango: str, origen: str, departamento: str, materias: list[str], modifica: list[tuple[str, str]]) -> str:
    return (
        f"<documento><metadatos><origen_legislativo>{origen}</origen_legislativo><departamento>{departamento}</departamento>"
        f"<rango>{rango}</rango></metadatos><analisis><materias>"
        + "".join(f"<materia>{m}</materia>" for m in materias)
        + "</materias><referencias><anteriores>"
        + "".join(
            f'<anterior referencia="{i}"><palabra>MODIFICA</palabra><texto>{t}</texto></anterior>' for i, t in modifica
        )
        + "</anteriores></referencias></analisis></documento>"
    )


VIVIENDA = (
    "1",
    "JEFATURA DEL ESTADO",
    "Vivienda",
    "BOE-A-2026-20823",
    "Real Decreto-ley 29/2026, de medidas urgentes para la vivienda asequible.",
)
XML_VIVIENDA = _xml(
    "Real Decreto-ley", "Estatal", "Jefatura del Estado",
    ["Arrendamientos urbanos", "Impuesto sobre la Renta de las Personas Físicas", "Bienes inmuebles", "Viviendas"],
    [(LIRPF, "determinados preceptos de la Ley 35/2006"), ("BOE-A-2000-323", "la Ley 1/2000")],
)  # fmt: skip
CLM = (
    "1",
    "COMUNIDAD AUTÓNOMA DE CASTILLA-LA MANCHA",
    "Medidas",
    LEY_CLM + "-X",
    "Ley 1/2026, de Medidas Administrativas y Tributarias de Castilla-La Mancha.",
)
XML_CLM = _xml(
    "Ley",
    "Autonómico",
    "Comunidad Autónoma de Castilla-La Mancha",
    ["Impuesto sobre la Renta de las Personas Físicas"],
    [(LEY_CLM, "la Ley 8/2013")],
)
MENORES = (
    "1",
    "COMUNIDAD AUTÓNOMA DEL PAÍS VASCO",
    "Menores",
    "BOE-A-2026-1",
    "Ley 2/2024, de Infancia y Adolescencia.",
)
XML_MENORES = _xml(
    "Ley", "Autonómico", "Comunidad Autónoma del País Vasco", ["Menores", "Juventud", "Ministerio Fiscal"], []
)
CONVENIO = (
    "3",
    "MINISTERIO DE HACIENDA",
    "Convenios",
    "BOE-A-2026-2",
    "Resolución por la que se publica el Convenio con la Agencia Tributaria.",
)
MODELO = (
    "3",
    "MINISTERIO DE HACIENDA",
    "Impuestos",
    "BOE-A-2026-3",
    "Resolución sobre la autoliquidación del Impuesto sobre el Valor Añadido.",
)
XMLS = {VIVIENDA[3]: XML_VIVIENDA, CLM[3]: XML_CLM, MENORES[3]: XML_MENORES}


def _revisar(*items, xmls=XMLS, **kw):
    return revisar(
        [DIA],
        sumario_de=lambda _d: _sumario(*items),
        xml_de=lambda boe_id: xmls.get(boe_id),
        vigiladas=frozenset({LIRPF, LEY_CLM}),
        hoy=kw.pop("hoy", date(2026, 11, 12)),
        **kw,
    )


def test_summary_is_flattened_for_sections_one_and_three_only() -> None:
    payload = _sumario(VIVIENDA, CONVENIO, ("5A", "X", "Y", "BOE-B-1", "Anuncio de licitación de tributos"))
    items = items_del_sumario(payload, DIA)
    assert [(i.seccion, i.boe_id, i.epigrafe) for i in items] == [
        ("1", VIVIENDA[3], "Vivienda"),
        ("3", "BOE-A-2026-2", "Convenios"),
    ]


def test_a_tax_change_hidden_in_a_housing_decree_is_found_by_its_analysis() -> None:
    item = items_del_sumario(_sumario(VIVIENDA), DIA)[0]
    assert clasificar(item, None) is None  # nothing in the title says "tax"
    novedad = clasificar(item, leer_analisis(XML_VIVIENDA, frozenset({LIRPF})), vigiladas=frozenset({LIRPF}))
    assert novedad is not None and novedad.ambito == "estatal" and novedad.relevancia == "alta"
    assert novedad.tributos == ["IRPF"]  # "Bienes inmuebles" alone is not a local tax
    assert [(m.boe_id, m.parametros) for m in novedad.modifica] == [(LIRPF, True)]
    assert novedad.analizada


def test_regional_laws_are_attributed_to_their_community() -> None:
    revision = _revisar(CLM, MENORES)
    (novedad,) = revision.novedades  # the children's act is not tax law, "Ministerio Fiscal" notwithstanding
    assert (novedad.ambito, novedad.comunidad, novedad.comunidad_nombre) == (
        "autonomico",
        "castilla_la_mancha",
        "Castilla-La Mancha",
    )
    assert novedad.modifica[0].parametros and novedad.relevancia == "alta"


def test_section_three_is_judged_by_title_and_noise_is_dropped() -> None:
    revision = _revisar(CONVENIO, MODELO)
    (novedad,) = revision.novedades
    assert novedad.boe_id == "BOE-A-2026-3" and novedad.tributos == ["IVA"] and not novedad.analizada
    assert novedad.relevancia == "baja"


def test_items_without_analysis_wait_and_are_read_again() -> None:
    primera = _revisar(VIVIENDA, xmls={}, hoy=date(2026, 10, 8))
    assert primera.novedades == [] and [p.boe_id for p in primera.pendientes] == [VIVIENDA[3]]
    # Days later the analysis is out: the pending item is classified without re-reading its day.
    segunda = revisar(
        [],
        sumario_de=lambda _d: None,
        xml_de=XMLS.get,
        vigiladas=frozenset({LIRPF}),
        pendientes=primera.pendientes,
        hoy=date(2026, 10, 12),
    )
    assert [n.boe_id for n in segunda.novedades] == [VIVIENDA[3]] and segunda.pendientes == []
    # After the waiting period an item with no analysis is dropped, not kept forever.
    assert _revisar(VIVIENDA, xmls={}, hoy=date(2026, 12, 1)).pendientes == []


def test_days_without_an_issue_and_failures_are_reported() -> None:
    def sumario(dia: date):
        if dia.day == 8:
            return None
        raise OSError("sin conexión")

    revision = revisar([date(2026, 11, 8), date(2026, 11, 9)], sumario_de=sumario, xml_de=lambda _i: None)
    assert revision.dias_sin_boletin == [date(2026, 11, 8)] and "sin conexión" in revision.errores[0]


def test_notifications_reach_only_the_profiles_concerned(tmp_path: Path) -> None:
    store = NotificationStore(str(tmp_path / "n.db"))
    manchego = make_case("clm", comunidad_autonoma="Castilla-La Mancha", rendimiento_trabajo_bruto=30000.0)
    madrileno = make_case("mad", comunidad_autonoma="Madrid", rendimiento_trabajo_bruto=30000.0)
    regional, estatal = _revisar(CLM, VIVIENDA).novedades

    assert notificar_novedad(regional, [manchego, madrileno], store) == 1
    (aviso,) = store.list("clm")
    assert aviso.title.startswith("Norma nueva: Ley 1/2026") and "Resides en Castilla-La Mancha." in aviso.body
    assert aviso.severity == "aviso" and "todavía no se ha analizado" in aviso.body

    # The State decree touches the IRPF Act: everyone is told, once.
    assert notificar_novedad(estatal, [manchego, madrileno], store) == 2
    assert notificar_novedad(estatal, [manchego, madrileno], store) == 0
    # Lower relevance is logged by the caller but never notified.
    assert (
        notificar_novedad(estatal.model_copy(update={"relevancia": "media", "boe_id": "otro"}), [madrileno], store) == 0
    )


def test_cli_keeps_a_register_and_resumes_from_the_last_day(tmp_path: Path, monkeypatch) -> None:
    pedidos: list[date] = []

    def sumario(dia: date):
        pedidos.append(dia)
        return _sumario(VIVIENDA) if dia == DIA else None

    monkeypatch.setattr(cli_module, "_sumario", sumario)
    monkeypatch.setattr(cli_module, "_xml", lambda boe_id: XMLS.get(boe_id))
    monkeypatch.setattr(cli_module, "hoy_en_espana", lambda: date(2026, 11, 12))
    args = ["boe-novedades", "--output-dir", str(tmp_path), "--no-notify"]

    primera = CliRunner().invoke(cli_module.cli, [*args, "--desde", "2026-10-06", "--hasta", "2026-10-08"])
    assert primera.exit_code == 0, primera.output
    informe = json.loads(primera.output)
    assert informe["novedades"] == 1 and informe["destacadas"][0]["modifica_vigiladas"] == [LIRPF]
    assert VIVIENDA[3] in json.loads((tmp_path / "_boe_novedades.json").read_text())

    pedidos.clear()
    segunda = CliRunner().invoke(cli_module.cli, [*args, "--hasta", "2026-10-10"])
    assert segunda.exit_code == 0 and pedidos == [date(2026, 10, 9), date(2026, 10, 10)]
    assert json.loads(segunda.output)["nuevas_en_el_registro"] == 0


def test_item_model_round_trips_through_the_state_file() -> None:
    item = ItemSumario(boe_id="BOE-A-1", fecha=DIA, seccion="1", departamento="D", titulo="T")
    assert ItemSumario(**item.model_dump(mode="json")) == item
