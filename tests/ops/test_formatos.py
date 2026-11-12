"""Format watch: canaries, baseline, status, samples, and how it surfaces in ``ops``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json

from click.testing import CliRunner
import formatos_util as util
import pytest

from hacienda_gpt import settings
from hacienda_gpt.cli import formatos as cli_formatos
from hacienda_gpt.cli import ops as ops_module
from hacienda_gpt.cli.ops import cli
from hacienda_gpt.ops import formatos, formatos_agente, vigia
from hacienda_gpt.ops.formatos import Fallo, Fuente, Lectura, Observacion, calcular_estado
from hacienda_gpt.proactive import novedades

SUMARIO = "boe_sumario_20261110.json"
XML = "boe_documento_BOE-A-2023-26452.xml"
HTML = "boe_consolidado_BOE-A-2006-20764_recorte.html"
META = "boe_metadatos_BOE-A-2006-20764.json"
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
def vigilancia(tmp_path, monkeypatch):
    """The watch switched on, writing under ``tmp_path``; no channels, no healthcheck, no network."""
    monkeypatch.setattr(settings, "FORMAT_WATCH", True)
    monkeypatch.setattr(settings, "FORMAT_STATE_PATH", str(tmp_path / "estado.json"))
    monkeypatch.setattr(settings, "FORMAT_DIR", str(tmp_path / "formatos"))
    monkeypatch.setattr(settings, "JOBS_STATE_PATH", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")

    def no_network(*_args, **_kwargs):
        raise AssertionError("the tests must not touch the network")

    monkeypatch.setattr(vigia.urllib.request, "urlopen", no_network)
    return tmp_path


def _canarios(clave: str, crudo: str, contexto: str = "") -> set[str]:
    fuente = formatos.fuente_de(clave)
    reglas = formatos.reglas_de(clave, fuente.reglas) if fuente.reglas is not None else None
    _, lectura = formatos.evaluar(fuente, reglas, crudo, contexto)
    return {fallo.canario for fallo in lectura.fallos if fallo.gravedad == "grave"}


# --------------------------------------------------------------------------- #
# Canaries, source by source: a good payload passes, a drifted one is caught
# --------------------------------------------------------------------------- #


def test_sumario_canaries() -> None:
    bueno = util.leer(SUMARIO)

    assert _canarios("boe_sumario", bueno, "2026-11-10") == set()
    # The key that holds the items is renamed: the parser finds nothing, as on a day with no dispositions.
    assert "items" in _canarios("boe_sumario", bueno.replace('"item"', '"disposicion"'), "2026-11-10")
    # The id key is renamed.
    assert "items" in _canarios("boe_sumario", bueno.replace('"identificador"', '"id"'), "2026-11-10")
    # Section codes change ("1" → "I"): there are items, none in the general section, on a Tuesday.
    sin_general = bueno.replace('"codigo": "1"', '"codigo": "3"')
    assert _canarios("boe_sumario", sin_general, "2026-11-10") == {"seccion_1"}
    # …which on a Saturday is only a warning.
    assert _canarios("boe_sumario", sin_general, "2026-11-14") == set()
    # An error envelope, an HTML error page.
    assert "estado_api" in _canarios("boe_sumario", '{"status": {"code": "500"}, "data": {}}', "2026-11-10")
    assert "lectura" in _canarios("boe_sumario", "<html>Server-unavailable!</html>", "2026-11-10")
    # Ids of an unknown shape.
    assert "identificadores" in _canarios("boe_sumario", bueno.replace("BOE-A-2026-", "DISP/2026/"), "2026-11-10")


def test_documento_xml_canaries() -> None:
    bueno = util.leer(XML)

    assert _canarios("boe_documento_xml", bueno, "BOE-A-2023-26452") == set()
    assert "identificador" in _canarios("boe_documento_xml", bueno, "BOE-A-2000-1")
    assert "identificador" in _canarios("boe_documento_xml", bueno.replace("metadatos>", "meta>"), "BOE-A-2023-26452")
    assert "lectura" in _canarios("boe_documento_xml", bueno[:500], "BOE-A-2023-26452")
    # One disposition without analysis is normal…
    sin_analisis = bueno.replace("analisis>", "analisis_juridico>")
    assert _canarios("boe_documento_xml", sin_analisis, "BOE-A-2023-26452") == set()
    # …none among everything read in a run is not.
    assert _canarios("boe_documento_xml", sin_analisis, "cobertura:BOE-A-2023-26452:40") == {"analisis"}
    assert _canarios("boe_documento_xml", bueno, "cobertura:BOE-A-2023-26452:40") == set()


def test_metadatos_canaries() -> None:
    bueno = util.leer(META)

    assert _canarios("boe_metadatos", bueno, "BOE-A-2006-20764") == set()
    assert _canarios("boe_metadatos", bueno, "BOE-A-1999-1") == {"identificador"}
    assert "fecha_actualizacion" in _canarios(
        "boe_metadatos", bueno.replace("fecha_actualizacion", "ultima_modificacion"), "BOE-A-2006-20764"
    )
    assert "fecha_actualizacion" in _canarios(
        "boe_metadatos", bueno.replace("20261111T094003Z", "11/11/2026 09:40"), "BOE-A-2006-20764"
    )
    assert _canarios("boe_metadatos", '{"status": {"code": "404"}, "data": []}', "BOE-A-2006-20764") == {"lectura"}


def test_consolidado_canaries() -> None:
    bueno = util.leer(HTML)

    assert _canarios("boe_consolidado_html", bueno) == set()
    # Headings in another wording: no block is recognised.
    assert _canarios("boe_consolidado_html", bueno.replace("Artículo ", "Art. ")) == {"bloques"}
    # The version line in another wording: blocks are found, none has a date.
    assert _canarios("boe_consolidado_html", bueno.replace("publicado el", "publicación:").replace("publicada el", "publicación:")) == {"versiones"}  # fmt: skip
    # An error page.
    assert _canarios("boe_consolidado_html", "<html><body><p>Servicio no disponible</p></body></html>") == {"bloques"}


def test_consolidado_markup_change_is_only_a_warning() -> None:
    fuente = formatos.fuente_de("boe_consolidado_html")
    reglas = formatos.reglas_de("boe_consolidado_html", fuente.reglas)
    _, lectura = formatos.evaluar(fuente, reglas, util.leer(HTML).replace('class="bloque"', 'class="blk"'), "")

    assert [(f.canario, f.gravedad) for f in lectura.fallos] == [("marcadores", "leve")]


def test_boletin_and_teac_canaries() -> None:
    listado = (util.FIXTURES / "boletines" / "bocm_listing.html").read_text(encoding="utf-8")
    assert _canarios("boletin_madrid_bocm", listado, "IRPF") == set()
    assert _canarios("boletin_madrid_bocm", listado.replace('class="resultado"', 'class="hit"'), "IRPF") == {
        "resultados"
    }
    assert _canarios("boletin_madrid_bocm", listado.replace("<h3>", "<p>").replace("</h3>", "</p>"), "IRPF") == {
        "titulos"
    }

    for nombre in ("criterio_dl.html", "criterio_tabla.html"):
        ficha = (util.FIXTURES / "teac" / nombre).read_text(encoding="utf-8")
        assert _canarios("teac_criterio", ficha) == set(), nombre
    ficha = (util.FIXTURES / "teac" / "criterio_dl.html").read_text(encoding="utf-8")
    rota = ficha.replace("<dt>", "<div class='k'>").replace("</dt>", "</div>").replace("<dd>", "<div class='v'>").replace("</dd>", "</div>")  # fmt: skip
    assert {"criterio", "campos"} <= _canarios("teac_criterio", rota)


def test_missing_summary_on_a_weekday_is_flagged_but_not_on_a_sunday(vigilancia) -> None:
    assert formatos.observar("boe_sumario_dias", None, extraccion={"dia": "2026-11-08", "hay": False}).ok  # Sunday
    assert formatos.observar("boe_sumario_dias", None, extraccion={"dia": "2026-11-09", "hay": True}).ok
    martes = formatos.observar("boe_sumario_dias", None, extraccion={"dia": "2026-11-10", "hay": False})
    assert not martes.ok
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario_dias"] == "degradada"
    formatos.observar("boe_sumario_dias", None, extraccion={"dia": "2026-11-11", "hay": False})
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario_dias"] == "rota"


# --------------------------------------------------------------------------- #
# Status and baseline
# --------------------------------------------------------------------------- #


def _obs(ok: bool, gravedad: str = "grave", **metricas: float) -> Observacion:
    fallos = [] if ok else [Fallo(canario="x", gravedad=gravedad, detalle="d")]  # type: ignore[arg-type]
    return Observacion(momento=T0.isoformat(), ok=ok, fallos=fallos, metricas=metricas)


def test_status_from_consecutive_reads() -> None:
    assert calcular_estado([]) == "sin_datos"
    assert calcular_estado([_obs(True)]) == "ok"
    assert calcular_estado([_obs(True), _obs(False)]) == "degradada"
    assert calcular_estado([_obs(True), _obs(False), _obs(False)]) == "rota"
    assert calcular_estado([_obs(False)]) == "rota"  # never read well
    assert calcular_estado([_obs(False), _obs(False), _obs(True)]) == "ok"
    # Warnings alone never make a source "rota".
    assert calcular_estado([_obs(True), *[_obs(False, "leve")] * 5]) == "degradada"


def test_status_from_failure_rate_for_documents() -> None:
    assert calcular_estado([_obs(False, "leve")] * 3, "tasa") == "ok"  # too few to say
    assert calcular_estado([*[_obs(True)] * 2, *[_obs(False, "leve")] * 2], "tasa") == "degradada"
    assert calcular_estado([_obs(True), *[_obs(False, "leve")] * 5], "tasa") == "rota"
    assert calcular_estado([*[_obs(True)] * 8, *[_obs(False, "leve")] * 2], "tasa") == "ok"


def test_a_drop_against_the_recent_median_degrades_the_source(vigilancia) -> None:
    fuente = formatos.registrar(
        Fuente(
            clave="prueba_caida",
            descripcion="fuente de prueba",
            expectativas=(),
            canarios=lambda datos, muestra, contexto, reglas: Lectura(metricas={"filas": float(datos)}),
            caidas=(("filas", 0.6),),
            guarda_muestras=False,
        )
    )
    try:
        for n in (100, 110, 90, 105):
            assert formatos.observar(fuente.clave, None, extraccion=n).ok
        # Four good reads are not a baseline yet: nothing is compared.
        assert formatos.observar(fuente.clave, None, extraccion=95).ok
        assert formatos.observar(fuente.clave, None, extraccion=60).ok  # within the tolerance
        caida = formatos.observar(fuente.clave, None, extraccion=30)

        assert [f.canario for f in caida.fallos] == ["caida_filas"] and caida.fallos[0].gravedad == "leve"
        assert {e.fuente: e.estado for e in formatos.resumen()}["prueba_caida"] == "degradada"
        # The failed read does not enter the baseline; a normal one recovers the source.
        assert formatos.observar(fuente.clave, None, extraccion=98).ok
        assert {e.fuente: e.estado for e in formatos.resumen()}["prueba_caida"] == "ok"
    finally:
        formatos._REGISTRO.pop(fuente.clave, None)


# --------------------------------------------------------------------------- #
# Observing: state, samples, and never getting in the parser's way
# --------------------------------------------------------------------------- #


def test_observing_keeps_a_rotating_good_sample_and_the_latest_bad_one(vigilancia) -> None:
    bueno = util.leer(SUMARIO)

    assert formatos.observar("boe_sumario", bueno, contexto="2026-11-10", ahora=T0).ok
    buena = formatos.leer_muestra("boe_sumario", "buena")
    assert buena is not None and buena.spec_version == 1 and len(buena.esperado) == 2
    # Within a day the good sample is not rewritten; after it, the previous one is kept too.
    formatos.observar("boe_sumario", bueno, contexto="2026-11-10", ahora=T0 + timedelta(hours=2))
    assert formatos.leer_muestra("boe_sumario", "buena").capturada == T0.isoformat()
    formatos.observar("boe_sumario", util.leer("boe_sumario_20261112.json"), contexto="2026-11-12", ahora=T0 + timedelta(days=2))  # fmt: skip
    assert formatos.leer_muestra("boe_sumario", "buena").contexto == "2026-11-12"
    assert formatos.leer_muestra("boe_sumario", "buena_anterior").contexto == "2026-11-10"

    malo = bueno.replace('"item"', '"disposicion"')
    observacion = formatos.observar("boe_sumario", malo, contexto="2026-11-13", ahora=T0 + timedelta(days=3))
    assert not observacion.ok
    mala = formatos.leer_muestra("boe_sumario", "mala")
    assert mala is not None and '"disposicion"' in mala.texto and {f.canario for f in mala.fallos} == {"items"}
    # The good samples are untouched by a bad read.
    assert formatos.leer_muestra("boe_sumario", "buena").contexto == "2026-11-12"

    estado = {e.fuente: e for e in formatos.resumen()}["boe_sumario"]
    assert estado.estado == "degradada" and estado.fallos[0].canario == "items"
    assert any("boe_sumario" in problema and "DEGRADADA" in problema for problema in formatos.problemas())


def test_observing_never_raises_and_does_nothing_when_switched_off(vigilancia, monkeypatch) -> None:
    assert formatos.observar("no_existe", "x") is None
    monkeypatch.setattr(settings, "FORMAT_STATE_PATH", "/proc/no/se/puede/escribir.json")
    assert formatos.observar("boe_sumario", util.leer(SUMARIO), contexto="2026-11-10") is None

    monkeypatch.setattr(settings, "FORMAT_STATE_PATH", str(vigilancia / "otro.json"))
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    assert formatos.observar("boe_sumario", util.leer(SUMARIO), contexto="2026-11-10") is None
    assert not (vigilancia / "otro.json").exists()


def test_an_unreadable_state_file_is_set_aside(vigilancia) -> None:
    (vigilancia / "estado.json").write_text("{roto")

    assert formatos.problemas() and "no se puede leer" in formatos.problemas()[0]
    assert formatos.observar("boe_metadatos", util.leer(META), contexto="BOE-A-2006-20764").ok
    assert (vigilancia / "estado.json.corrupt").exists() and formatos.problemas() == []


# --------------------------------------------------------------------------- #
# The real parsers report, and return exactly what they returned
# --------------------------------------------------------------------------- #


def _revisar(sumario_de, xml_de, hoy=date(2026, 11, 13)):
    return novedades.revisar([date(2026, 11, 10), date(2026, 11, 11)], sumario_de=sumario_de, xml_de=xml_de, hoy=hoy)


def test_revisar_reports_what_it_reads_without_changing_its_result(vigilancia, monkeypatch) -> None:
    payload = json.loads(util.leer(SUMARIO))
    xml = util.leer(XML)

    def sumario_de(dia: date):
        return payload if dia == date(2026, 11, 10) else None

    con_vigilancia = _revisar(sumario_de, lambda _id: xml)
    monkeypatch.setattr(settings, "FORMAT_WATCH", False)
    sin_vigilancia = _revisar(sumario_de, lambda _id: xml)

    assert con_vigilancia == sin_vigilancia
    estados = {e.fuente: e.estado for e in formatos.resumen()}
    # The XML fixture belongs to another disposition: the id canary notices.
    assert estados["boe_sumario"] == "ok" and estados["boe_documento_xml"] == "rota"
    # 11 November 2026 is a Wednesday with no summary.
    assert estados["boe_sumario_dias"] == "degradada"


def test_revisar_flags_a_summary_that_parses_to_nothing(vigilancia) -> None:
    payload = json.loads(util.leer(SUMARIO).replace('"item"', '"disposicion"'))

    revision = _revisar(lambda _dia: payload, lambda _id: None)

    # Today this is indistinguishable from two quiet days…
    assert revision.novedades == [] and revision.errores == [] and revision.dias_sin_boletin == []
    # …except for the watch.
    estado = {e.fuente: e for e in formatos.resumen()}["boe_sumario"]
    assert estado.estado == "rota" and estado.fallos[0].canario == "items"


def test_revisar_flags_a_run_where_no_disposition_has_an_analysis(vigilancia, monkeypatch) -> None:
    monkeypatch.setattr(novedades, "MIN_LEIDOS_COBERTURA", 2)
    payload = json.loads(util.leer(SUMARIO))
    plantilla = util.leer(XML).replace("analisis>", "analisis_juridico>")

    _revisar(lambda dia: payload if dia == date(2026, 11, 10) else None, lambda boe_id: plantilla.replace("BOE-A-2023-26452", boe_id))  # fmt: skip

    estado = {e.fuente: e for e in formatos.resumen()}["boe_documento_xml"]
    assert estado.estado == "degradada" and [f.canario for f in estado.fallos] == ["analisis"]
    assert formatos.leer_muestra("boe_documento_xml", "mala").contexto.startswith("cobertura:")


def test_documents_reader_reports_counters_only(vigilancia) -> None:
    from hacienda_gpt.tax.documentos import analizar_documento

    texto = (
        "CERTIFICADO DE RETENCIONES\nPerceptor: Ana Pérez Gómez NIF 12345678Z\nRetribuciones dinerarias 41.250,37\n" * 2
    )
    for _ in range(5):
        proveedor = util.ProveedorGuionizado(
            [{"tipo_documento": "Certificado de retenciones", "hechos": [{"name": "retenciones", "value": "7000", "evidencia": "no está en el documento"}]}]  # fmt: skip
        )
        analizar_documento(proveedor, filename="cert.txt", data=texto.encode())

    estado = {e.fuente: e for e in formatos.resumen()}["doc_certificado_retenciones"]
    assert estado.estado == "rota" and estado.fallos[0].canario == "todo_descartado"
    en_disco = "".join(p.read_text() for p in vigilancia.rglob("*") if p.is_file())
    assert "retenciones" in en_disco
    for dato in ("Ana", "Pérez", "12345678Z", "41.250,37", "7000"):
        assert dato not in en_disco
    # Skeletons are off by default: nothing derived from the text is kept.
    assert not (vigilancia / "formatos" / "esqueletos").exists()


# --------------------------------------------------------------------------- #
# ops doctor and ops formatos check
# --------------------------------------------------------------------------- #


class FakeCanal:
    nombre = "fake"

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    def enviar(self, asunto: str, cuerpo: str) -> None:
        self.enviados.append((asunto, cuerpo))


def test_doctor_reports_a_broken_format_through_the_usual_channels(vigilancia, monkeypatch) -> None:
    canal = FakeCanal()
    monkeypatch.setattr(ops_module, "canales_configurados", lambda: [canal])
    sin_limites = [arg for nombre in ops_module.MAX_HORAS_POR_DEFECTO for arg in ("--max-horas", f"{nombre}=0")]

    assert CliRunner().invoke(cli, ["doctor", *sin_limites]).exit_code == 0

    formatos.observar("boe_sumario", util.leer(SUMARIO).replace('"item"', '"disposicion"'), contexto="2026-11-10")
    result = CliRunner().invoke(cli, ["doctor", *sin_limites])

    assert result.exit_code == 1
    problemas = json.loads(result.output)["problemas"]
    assert len(problemas) == 1 and "formato boe_sumario" in problemas[0] and "ROTA" in problemas[0]
    assert canal.enviados and "boe_sumario" in canal.enviados[0][1]


def test_check_reports_the_deterministic_status_and_skips_the_llm_without_a_key(vigilancia, monkeypatch) -> None:
    canal = FakeCanal()
    monkeypatch.setattr(cli_formatos, "_canales", lambda: [canal])
    monkeypatch.setattr(formatos_agente, "hay_llm", lambda: False)
    monkeypatch.setattr(cli_formatos, "_proveedores", lambda: pytest.fail("no provider must be built without a key"))
    formatos.observar("boe_metadatos", util.leer(META), contexto="BOE-A-2006-20764")
    formatos.observar("boe_sumario", util.leer(SUMARIO).replace('"item"', '"disposicion"'), contexto="2026-11-10")

    result = CliRunner().invoke(cli, ["formatos", "check", "--diagnose", "--fail-on-problem"])

    assert result.exit_code == 1, result.output
    salida = json.loads(result.output)
    assert {f["fuente"]: f["estado"] for f in salida["fuentes"]} == {"boe_metadatos": "ok", "boe_sumario": "rota"}
    assert "teac_criterio" in salida["sin_datos"]
    assert salida["diagnostico"]["pendientes"] == ["boe_sumario"]
    assert salida["diagnostico"]["llm"].startswith("no configurado") and salida["diagnostico"]["resultados"] == []
    assert salida["avisado_por"] == ["fake"] and canal.enviados[0][0] == cli_formatos.ASUNTO_FORMATOS
    # The check itself is a watched job.
    assert vigia.leer_estado()["formatos"]["last_ok"] is True


def test_check_is_quiet_when_everything_is_understood(vigilancia, monkeypatch) -> None:
    monkeypatch.setattr(cli_formatos, "_canales", lambda: pytest.fail("nothing to send"))
    formatos.observar("boe_metadatos", util.leer(META), contexto="BOE-A-2006-20764")

    result = CliRunner().invoke(cli, ["formatos", "check", "--fail-on-problem"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["problemas"] == []


def test_stale_price_table_is_mentioned() -> None:
    assert cli_formatos.avisos_de_datos_propios(date(2026, 11, 13)) == []
    assert "tabla de precios" in cli_formatos.avisos_de_datos_propios(date(2027, 6, 1))[0]


def test_sources_command_lists_expectations_and_specs(vigilancia) -> None:
    result = CliRunner().invoke(cli, ["formatos", "sources"])

    assert result.exit_code == 0, result.output
    fuentes = {f["fuente"]: f for f in json.loads(result.output)}
    assert fuentes["boe_sumario"]["spec"] == {"version": 1, "origen": "incorporada"}
    assert fuentes["boe_sumario"]["expectativas"] and fuentes["doc_nomina"]["spec"] is None


def test_dgt_search_canaries() -> None:
    def graves(**datos) -> set[str]:
        lectura = formatos.fuente_de("dgt_busqueda").canarios(datos, "", "", None)
        return {f.canario for f in lectura.fallos if f.gravedad == "grave"}

    assert graves(filas=20, con_numero=20, total_paginas=True, query=True) == set()
    # A month with no consultas is still a recognisable page.
    assert graves(filas=0, con_numero=0, total_paginas=True, query=True) == set()
    assert graves(filas=0, con_numero=0, total_paginas=False, query=False) == {"pagina"}
    assert graves(filas=20, con_numero=20, total_paginas=True, query=False) == {"query"}


def _descargas(sumario: str | None):
    def descargar(url: str, _accept: str) -> str | None:
        if "/sumario/" in url:
            return sumario if url.endswith("20261111") else None
        if "xml.php" in url:
            return util.leer(XML).replace("BOE-A-2023-26452", url.rsplit("=", 1)[1])
        if url.endswith("/metadatos"):
            return util.leer(META)
        return util.leer(HTML)

    return descargar


def test_probe_reads_one_payload_of_each_boe_format_with_the_real_parsers(vigilancia) -> None:
    errores = cli_formatos.sondear(date(2026, 11, 12), _descargas(util.leer(SUMARIO)))

    assert errores == []
    estados = {e.fuente: e.estado for e in formatos.resumen() if e.estado != "sin_datos"}
    assert estados == {
        "boe_consolidado_html": "ok",
        "boe_documento_xml": "ok",
        "boe_metadatos": "ok",
        "boe_sumario": "ok",
        "boe_sumario_dias": "ok",
    }


def test_probe_notices_a_drifted_summary_and_survives_errors(vigilancia) -> None:
    cli_formatos.sondear(date(2026, 11, 12), _descargas(util.leer(SUMARIO).replace('"item"', '"disposicion"')))
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario"] == "rota"

    def caida(_url: str, _accept: str) -> str | None:
        raise ConnectionError("sin red")

    errores = cli_formatos.sondear(date(2026, 11, 12), caida)
    assert len(errores) == 9 and all("sin red" in error for error in errores)
    # An unreachable BOE is an error to report, not "a weekday without a summary".
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario_dias"] == "ok"
