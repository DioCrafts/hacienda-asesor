from datetime import date

import pytest

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.proactive.impacto import (
    TAGS,
    CambioNormativo,
    analizar_cambio,
    articulos_modificados,
    clave_evento,
    notificar_cambio,
    parse_fecha_actualizacion,
    perfil_afectado,
    preparar_eventos,
    procesar_eventos,
)
from hacienda_gpt.proactive.store import NotificationStore
from . import make_case

# Synthetic page shaped like a BOE consolidated law (act.php). The real markup
# could not be fetched here, so the parser is only required to work from the text.
LIRPF_HTML = """<!DOCTYPE html>
<html><head><title>BOE-A-2006-20764</title><style>.bloque { margin: 0 }</style>
<script>var x = "Artículo 999. Publicada el 01/01/2030";</script></head>
<body>
<div class="bloque" id="tii"><h4 class="titulo_num">TÍTULO II</h4><h4 class="titulo_tit">Determinación de la renta</h4></div>
<div class="bloque" id="a19">
  <p class="bloque">[Bloque 30: #a19]</p>
  <h5 class="articulo">Artículo 19. Rendimiento neto del trabajo.</h5>
  <p class="parrafo">1. El rendimiento neto del trabajo será el resultado de disminuir el rendimiento íntegro en el
  importe de los gastos deducibles.</p>
  <blockquote><p class="nota_pie">Se modifica el apartado 2 por el art. 1.9 de la Ley 26/2014, de 27 de noviembre.
  Ref. BOE-A-2014-12327</p></blockquote>
  <form><select><option>Última actualización, publicada el 28/11/2014, en vigor a partir del 01/01/2015.</option>
  <option>Texto original, publicado el 29/11/2006, en vigor a partir del 01/01/2007.</option></select></form>
</div>
<div class="bloque" id="a63">
  <p class="bloque">[Bloque 85: #a63]</p>
  <h5 class="articulo">Artículo 63. Escala general del Impuesto.</h5>
  <p class="parrafo">1. La parte de la base liquidable general que exceda del importe del mínimo personal y familiar
  será gravada de la siguiente forma:</p>
  <p class="parrafo">A la base liquidable general se le aplicarán los tipos que se indican en la siguiente escala.</p>
  <table><tr><td>300.000,00</td><td>En adelante</td><td>24,50</td></tr></table>
  <blockquote><p class="nota_pie">Se modifica el apartado 1, con efectos desde 1 de enero de 2021, por el art. 58 de
  la Ley 11/2020, de 30 de diciembre. Ref. BOE-A-2020-17339</p>
  <p class="nota_pie">Se modifica por el art. 1.39 de la Ley 26/2014, de 27 de noviembre. Ref. BOE-A-2014-12327</p>
  </blockquote>
  <blockquote><p>Seleccionar redacción:</p><ul>
  <li>Última actualización, publicada el 31/12/2020, en vigor a partir del 01/01/2021.</li>
  <li>Modificación publicada el 28/11/2014, en vigor a partir del 01/01/2015.</li>
  <li>Texto original, publicado el 29/11/2006, en vigor a partir del 01/01/2007.</li></ul></blockquote>
</div>
<div class="bloque" id="ci"><h4 class="capitulo_num">CAPÍTULO I</h4>
  <p>Texto de capítulo, publicado el 01/06/2021, que no pertenece a ningún artículo.</p></div>
<div class="bloque" id="a81bis">
  <h5 class="articulo">Artículo 81 bis. Deducciones por familia numerosa.</h5>
  <p class="parrafo">1. Los contribuyentes que realicen una actividad por cuenta propia o ajena podrán minorar la
  cuota diferencial del impuesto.</p>
  <blockquote><p>Texto original, publicado el 29/11/2006, en vigor a partir del 01/01/2007.</p></blockquote>
</div>
<div class="bloque" id="dacuadragesimaseptima">
  <h5 class="articulo">Disposición adicional quincuagésima octava. Rendimientos de actividades artísticas.</h5>
  <p class="parrafo">Los rendimientos del trabajo obtenidos por artistas tendrán una reducci&oacute;n del 30 por
  ciento cuando excedan del 130 por ciento de la media.</p>
  <blockquote><p class="nota_pie">Se añade por la disposición final 3 de la Ley 28/2022, de 21 de diciembre.
  Ref. BOE-A-2022-21739</p></blockquote>
  <blockquote><p>Texto añadido, publicado el 22/12/2022, en vigor a partir del 01/01/2023.</p></blockquote>
</div>
<div class="bloque" id="dfsegunda">
  <h5 class="articulo">Disposición final segunda. Entrada en vigor.</h5>
  <p class="parrafo">Esta Ley entrará en vigor el día 1 de enero de 2007.</p>
  <blockquote><p>Texto original, publicado el 29/11/2006.</p></blockquote>
</div>
</body></html>
"""


def test_only_blocks_published_after_the_date_are_returned() -> None:
    articulos = articulos_modificados(LIRPF_HTML, desde=date(2020, 6, 1))
    assert [a.titulo for a in articulos] == [
        "Artículo 63. Escala general del Impuesto.",
        "Disposición adicional quincuagésima octava. Rendimientos de actividades artísticas.",
    ]
    escala, artistas = articulos
    assert escala.publicado == date(2020, 12, 31) and escala.en_vigor == date(2021, 1, 1)
    assert escala.nota.startswith("Se modifica el apartado 1, con efectos desde 1 de enero de 2021, por el art. 58")
    assert "Ref. BOE-A-2020-17339" in escala.nota and "Ley 26/2014" in escala.nota
    assert escala.texto.startswith("1. La parte de la base liquidable general que exceda")
    assert "300.000,00" in escala.texto and "24,50" in escala.texto
    # Notes, version lines and selector labels are not part of the wording.
    for noise in ("Se modifica", "publicada el", "Seleccionar", "[Bloque"):
        assert noise not in escala.texto

    assert artistas.publicado == date(2022, 12, 22) and artistas.en_vigor == date(2023, 1, 1)
    assert artistas.nota.startswith("Se añade por la disposición final 3")
    assert "reducción del 30 por ciento" in artistas.texto  # entities decoded


def test_inline_markup_and_source_line_breaks_do_not_split_the_text() -> None:
    html = (
        "<h5>Artículo 7. Rentas exentas.</h5><p>Estarán <em>exentas</em> las\n   siguientes <a href='#'>rentas</a>:</p>"
        "<p class='nota_pie'>Se modifica la letra\ny) por la Ley 7/2024.</p><p>Última actualización, publicada el 21/12/2024</p>"
    )
    (articulo,) = articulos_modificados(html, desde=date(2024, 1, 1))
    assert articulo.texto == "Estarán exentas las siguientes rentas:"
    assert articulo.nota == "Se modifica la letra y) por la Ley 7/2024."
    assert articulo.publicado == date(2024, 12, 21) and articulo.en_vigor is None


def test_date_boundaries_and_trimming() -> None:
    # Strictly after: a block published exactly on `desde` was already known.
    assert [a.publicado for a in articulos_modificados(LIRPF_HTML, desde=date(2020, 12, 31))] == [date(2022, 12, 22)]
    assert articulos_modificados(LIRPF_HTML, desde=date(2022, 12, 22)) == []
    assert articulos_modificados("", desde=date(2000, 1, 1)) == []
    assert articulos_modificados("<p>Sin artículos, publicado el 01/01/2025</p>", desde=date(2000, 1, 1)) == []

    everything = articulos_modificados(LIRPF_HTML, desde=date(2000, 1, 1))
    assert len(everything) == 5
    final = everything[-1]
    assert final.titulo.startswith("Disposición final segunda") and final.en_vigor is None

    long_html = "<h5>Artículo 1. Largo.</h5>" + "<p>" + "palabra " * 1000 + "</p><p>Publicado el 01/02/2025.</p>"
    (largo,) = articulos_modificados(long_html, desde=date(2025, 1, 1))
    assert len(largo.texto) == 2500


# --------------------------------------------------------------------------- #
# analizar_cambio
# --------------------------------------------------------------------------- #


class FakeProvider:
    name, model = "fake", "fake"

    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete_json(self, *, system, user, schema, schema_name):
        self.calls.append({"system": system, "user": user, "schema": schema, "schema_name": schema_name})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _respuesta(**overrides) -> dict:
    base = {
        "resumen": "Cambia la escala estatal del IRPF.  Sube el tipo para las rentas más altas.",
        "en_vigor_desde": "1 de enero de 2021",
        "afecta_a": ["todos"],
        "relevancia": "alta",
        "cita_literal": "A la base liquidable general se le aplicarán los tipos\nque se indican en la siguiente ESCALA",
    }
    return {**base, **overrides}


ARTICULOS = articulos_modificados(LIRPF_HTML, desde=date(2020, 6, 1))


def test_analysis_is_kept_when_the_quote_is_in_the_text() -> None:
    provider = FakeProvider(_respuesta())
    cambio = analizar_cambio(
        provider, boe_id="BOE-A-2006-20764", titulo_norma="Ley 35/2006 del IRPF", articulos=ARTICULOS
    )

    assert cambio is not None
    assert cambio.resumen == "Cambia la escala estatal del IRPF. Sube el tipo para las rentas más altas."
    assert (
        cambio.afecta_a == ["todos"] and cambio.relevancia == "alta" and cambio.en_vigor_desde == "1 de enero de 2021"
    )
    assert cambio.actualizado == "2022-12-22"  # defaults to the latest publication date
    assert cambio.articulos[0].startswith("Disposición adicional") and len(cambio.articulos) == 2

    (call,) = provider.calls  # one call for the whole law
    assert call["schema_name"] == "cambio_normativo"
    assert call["schema"]["properties"]["afecta_a"]["items"]["enum"] == list(TAGS)
    assert "Artículo 63. Escala general del Impuesto." in call["user"] and "Ley 35/2006 del IRPF" in call["user"]
    assert "Se modifica el apartado 1" in call["user"]
    assert "No deduzcas efectos que el texto no diga" in call["system"] and "renumeración" in call["system"]
    assert all(tag in call["system"] for tag in TAGS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"cita_literal": "El tipo máximo sube al 60 por ciento para todos los contribuyentes"},  # invented
        {"cita_literal": "escala"},  # too short to prove anything
        {"cita_literal": ""},
        {"afecta_a": []},
        {"afecta_a": ["marcianos"]},  # outside the vocabulary
        {"relevancia": "enorme"},
        {"resumen": "  "},
    ],
)
def test_analysis_is_discarded_when_it_cannot_be_trusted(overrides) -> None:
    provider = FakeProvider(_respuesta(**overrides))
    assert analizar_cambio(provider, boe_id="BOE-A-2006-20764", titulo_norma="LIRPF", articulos=ARTICULOS) is None


def test_analysis_filters_unknown_tags_and_skips_empty_input() -> None:
    provider = FakeProvider(_respuesta(afecta_a=["inversores", "marcianos", "asalariados"]))
    cambio = analizar_cambio(
        provider, boe_id="X", titulo_norma="LIRPF", articulos=ARTICULOS, actualizado="20230101T000000Z"
    )
    assert cambio is not None and cambio.afecta_a == ["asalariados", "inversores"]
    assert cambio.actualizado == "20230101T000000Z"

    assert analizar_cambio(provider, boe_id="X", titulo_norma="LIRPF", articulos=[]) is None
    assert len(provider.calls) == 1  # nothing to analyse: no call

    with pytest.raises(ProviderError):
        analizar_cambio(FakeProvider(ProviderError("sin red")), boe_id="X", titulo_norma="L", articulos=ARTICULOS)


# --------------------------------------------------------------------------- #
# perfil_afectado
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tag", "facts", "not_affected"),
    [
        ("todos", {}, None),
        ("asalariados", {"categoria_contribuyente": "asalariado"}, {"categoria_contribuyente": "autonomo"}),
        (
            "asalariados",
            {"rendimiento_trabajo_bruto": 30000.0},
            {"rendimiento_trabajo_bruto": 30000.0, "categoria_contribuyente": "pensionista"},
        ),
        ("autonomos", {"alta_actividad_economica": True}, {"alta_actividad_economica": False}),
        ("autonomos", {"categoria_contribuyente": "autonomo"}, {"categoria_contribuyente": "asalariado"}),
        ("autonomos", {"rendimiento_actividad_neto": 12000.0}, {"rendimiento_actividad_neto": 0.0}),
        ("autonomos", {"regimen_cotizacion": "RETA"}, {"regimen_cotizacion": "general"}),
        ("pensionistas", {"categoria_contribuyente": "pensionista"}, {"rendimiento_trabajo_bruto": 20000.0}),
        ("arrendadores", {"rendimiento_alquiler_neto": 6000.0}, {"alquiler_vivienda_habitual": 9000.0}),
        ("inquilinos", {"alquiler_vivienda_habitual": 9000.0}, {"rendimiento_alquiler_neto": 6000.0}),
        ("inversores", {"rendimiento_capital_mobiliario": 300.0}, {"rendimiento_capital_mobiliario": 0.0}),
        ("inversores", {"ganancias_patrimoniales": -500.0}, {}),
        ("familias_con_hijos", {"num_descendientes": 2.0}, {"num_descendientes": 0.0}),
        ("propietarios_vivienda", {"pagos_vivienda_habitual_pre2013": 4000.0}, {"alquiler_vivienda_habitual": 1.0}),
        ("donantes", {"donativos": 150.0}, {"donativos": 0.0}),
        ("planes_de_pensiones", {"aportacion_plan_pensiones": 1500.0}, {"aportacion_plan_pensiones": 0.0}),
        ("bienes_en_el_extranjero", {"bienes_en_el_extranjero": True}, {"bienes_en_el_extranjero": False}),
        ("no_residentes", {"residencia_fiscal": "PT"}, {"residencia_fiscal": "ES"}),
        ("sociedades", {"categoria_contribuyente": "sociedad"}, {"categoria_contribuyente": "autonomo"}),
    ],
)
def test_each_tag_matches_its_profile(tag, facts, not_affected) -> None:
    reasons = perfil_afectado(facts, [tag])
    assert len(reasons) == 1 and reasons[0].endswith(".")
    if not_affected is not None:
        assert perfil_afectado(not_affected, [tag]) == []
    if tag != "todos":
        assert perfil_afectado({}, [tag]) == []  # an empty profile only matches "todos"


def test_profile_matching_combines_reasons_and_ignores_unknown_tags() -> None:
    facts = {"rendimiento_trabajo_bruto": 30000.0, "donativos": 100.0, "bienes_en_el_extranjero": "sí"}
    assert perfil_afectado(facts, ["donantes", "asalariados", "sociedades", "no_residentes", "inventado"]) == [
        "Tienes rendimientos del trabajo por cuenta ajena.",
        "Haces donativos.",
    ]
    assert perfil_afectado(facts, []) == []
    assert perfil_afectado(facts, ["bienes_en_el_extranjero"]) == []  # only a literal True counts


# --------------------------------------------------------------------------- #
# notificar_cambio / procesar_eventos
# --------------------------------------------------------------------------- #


def _cambio(**overrides) -> CambioNormativo:
    base = {
        "boe_id": "BOE-A-2006-20764",
        "titulo_norma": "Ley 35/2006, de 28 de noviembre, del Impuesto sobre la Renta de las Personas Físicas",
        "actualizado": "20201231T120000Z",
        "resumen": "Se reduce el límite de aportación a planes de pensiones individuales.",
        "en_vigor_desde": "1 de enero de 2021",
        "afecta_a": ["planes_de_pensiones", "autonomos"],
        "relevancia": "alta",
        "cita_literal": "El conjunto de las reducciones no podrá exceder de 2.000 euros anuales",
        "articulos": ["Artículo 52. Límite de reducción."],
    }
    return CambioNormativo(**{**base, **overrides})


@pytest.fixture
def store(tmp_path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "db.sqlite3"))


CASES = [
    make_case("ahorrador", rendimiento_trabajo_bruto=40000, aportacion_plan_pensiones=3000),
    make_case("autonomo", alta_actividad_economica=True, aportacion_plan_pensiones=500),
    make_case("asalariado", rendimiento_trabajo_bruto=25000),
]


def test_only_affected_cases_are_notified_once(store) -> None:
    assert notificar_cambio(_cambio(), CASES, store) == 2
    assert store.list("asalariado") == []

    (n,) = store.list("ahorrador")
    assert n.kind == "cambio_normativo" and n.severity == "aviso" and n.amount is None and n.due_date is None
    assert n.key == "cambio_normativo:BOE-A-2006-20764:20201231T120000Z"
    assert n.title.startswith("Cambio normativo: Ley 35/2006, de 28 de noviembre")
    assert "Se reduce el límite de aportación" in n.body
    assert "**Por qué te lo cuento:** Aportas a un plan de pensiones." in n.body
    assert "«El conjunto de las reducciones no podrá exceder de 2.000 euros anuales»" in n.body
    assert "1 de enero de 2021" in n.body and "Artículo 52. Límite de reducción." in n.body
    assert n.actions[0] == "Pregunta al asesor cómo cambia tu situación con esta modificación."
    assert "BOE-A-2006-20764" in n.sources[0] and n.sources[1].endswith("act.php?id=BOE-A-2006-20764")

    both = store.list("autonomo")[0].body
    assert "Ejerces una actividad económica como autónomo. Aportas a un plan de pensiones." in both

    # Re-running the same revision never duplicates; a later revision notifies again.
    assert notificar_cambio(_cambio(), CASES, store) == 0
    assert notificar_cambio(_cambio(actualizado="20211229T120000Z", relevancia="media"), CASES, store) == 2
    assert store.list("ahorrador")[0].severity == "info"


def test_low_relevance_changes_are_never_notified(store) -> None:
    assert notificar_cambio(_cambio(relevancia="baja", afecta_a=["todos"]), CASES, store) == 0
    assert all(store.list(case.case_id) == [] for case in CASES)


def _evento(boe_id="BOE-A-2006-20764", previous="20200601T080000Z", new="20221222T090000Z", **extra) -> dict:
    return {
        "ts": "2022-12-22T10:00:00+00:00",
        "id": boe_id,
        "event": "changed" if previous else "new",
        "previous_actualizacion": previous,
        "new_actualizacion": new,
        "path": f"data/html/boe-seed/{boe_id}.html",
        "title": "Ley 35/2006 del IRPF",
        **extra,
    }


def test_dates_from_boe_watch_are_parsed() -> None:
    assert parse_fecha_actualizacion("20201231T113000Z") == date(2020, 12, 31)
    assert parse_fecha_actualizacion("2020-12-31T11:30:00+00:00") == date(2020, 12, 31)
    assert parse_fecha_actualizacion("31/12/2020") == date(2020, 12, 31)
    assert parse_fecha_actualizacion("") is None and parse_fecha_actualizacion(None) is None
    assert parse_fecha_actualizacion("ayer") is None


def test_procesar_eventos_makes_one_call_per_changed_law(store) -> None:
    provider = FakeProvider(_respuesta())
    eventos = [
        _evento(),
        _evento(boe_id="BOE-A-1992-28740", previous=None, new="20221201T000000Z"),  # first download
        _evento(boe_id="BOE-A-2004-4456", previous="20230101T000000Z", new="20230301T000000Z"),  # nothing newer
    ]
    html = {"BOE-A-2006-20764": LIRPF_HTML, "BOE-A-2004-4456": LIRPF_HTML, "BOE-A-1992-28740": LIRPF_HTML}

    summary = procesar_eventos(provider, eventos=eventos, html_por_boe_id=html, cases=CASES, store=store)

    assert len(provider.calls) == 1
    assert summary["eventos"] == 3 and summary["llamadas_llm"] == 1 and summary["analizados"] == 1
    assert summary["notificaciones"] == 3 and summary["errores"] == 0 and summary["pendientes"] == []
    assert sorted(summary["procesados"]) == sorted(clave_evento(e) for e in eventos)
    for case in CASES:  # "todos"
        (n,) = store.list(case.case_id)
        assert n.key == "cambio_normativo:BOE-A-2006-20764:20221222T090000Z"
        assert n.title == "Cambio normativo: Ley 35/2006 del IRPF"

    # Same batch again: the call is repeated (the CLI state file prevents that) but nobody is re-notified.
    again = procesar_eventos(provider, eventos=eventos, html_por_boe_id=html, cases=CASES, store=store)
    assert again["notificaciones"] == 0


def test_procesar_eventos_collapses_events_and_accepts_a_loader(store) -> None:
    provider = FakeProvider(_respuesta())
    eventos = [_evento(previous="20200601T080000Z", new="20201231T120000Z"), _evento(previous="20201231T120000Z")]
    asked: list[str] = []

    def loader(boe_id: str) -> str:
        asked.append(boe_id)
        return LIRPF_HTML

    preparacion = preparar_eventos(eventos, loader)
    (pendiente,) = preparacion.pendientes
    assert pendiente.desde == date(2020, 6, 1) and pendiente.actualizado == "20221222T090000Z"
    assert len(pendiente.articulos) == 2 and len(pendiente.eventos) == 2

    summary = procesar_eventos(provider, eventos=eventos, html_por_boe_id=loader, cases=CASES[:1], store=store)
    assert len(provider.calls) == 1 and summary["notificaciones"] == 1 and len(summary["procesados"]) == 2
    assert set(asked) == {"BOE-A-2006-20764"}


def test_procesar_eventos_keeps_failures_for_a_retry(store) -> None:
    eventos = [_evento(), _evento(boe_id="BOE-A-2004-4456")]
    html = {"BOE-A-2006-20764": LIRPF_HTML}  # the second law's HTML is missing

    failed = procesar_eventos(
        FakeProvider(ProviderError("sin red")), eventos=eventos, html_por_boe_id=html, cases=CASES, store=store
    )
    assert failed["errores"] == 2 and failed["notificaciones"] == 0 and failed["procesados"] == []
    assert sorted(failed["pendientes"]) == sorted(clave_evento(e) for e in eventos)

    # An untrustworthy answer is final: not retried, nobody notified.
    discarded = procesar_eventos(
        FakeProvider(_respuesta(cita_literal="Texto que la norma no contiene en ninguna parte")),
        eventos=eventos[:1],
        html_por_boe_id=html,
        cases=CASES,
        store=store,
    )
    assert discarded["descartados"] == 1 and discarded["procesados"] == [clave_evento(eventos[0])]
    assert all(store.list(case.case_id) == [] for case in CASES)
