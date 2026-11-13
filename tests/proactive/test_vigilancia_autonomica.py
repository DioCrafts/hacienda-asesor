"""Watching the regional laws the calculator's parameters come from."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from click.testing import CliRunner

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.proactive.impacto import CambioNormativo, notificar_cambio
from hacienda_gpt.proactive.store import NotificationStore
from hacienda_gpt.tax.fuentes import comunidad_de, desactualizadas, norma_de_parametros, normas_de_parametros
from tests.proactive import make_case

CATALOGO = Path(cli_module.__file__).resolve().parent / "boe_seed_catalog.json"
LEY_GALLEGA = "BOE-A-2011-18161"
LEY_IRPF = "BOE-A-2006-20764"


def test_every_parameter_source_is_in_the_watch_catalogue() -> None:
    vigiladas = {d["id"] for d in json.loads(CATALOGO.read_text())["documents"] if d.get("track", True)}
    normas = normas_de_parametros()
    # The IRPF Act, one law per community, and the laws behind the other calculators.
    assert len(normas) >= 25 and sum(n.comunidad is not None for n in normas) == 15
    assert {n.boe_id for n in normas} <= vigiladas
    assert norma_de_parametros(LEY_IRPF).comunidad is None
    assert norma_de_parametros(LEY_GALLEGA).comunidad == "galicia"
    assert comunidad_de("Xunta de Galicia") == "galicia" and comunidad_de("Navarra") is None


def test_stale_sources_are_those_updated_after_they_were_read() -> None:
    gallega = norma_de_parametros(LEY_GALLEGA)
    assert desactualizadas({LEY_GALLEGA: gallega.consultado_el}) == []
    (pendiente,) = desactualizadas({LEY_GALLEGA: date(2099, 1, 1), LEY_IRPF: date(2000, 1, 1)})
    assert pendiente[0].boe_id == LEY_GALLEGA and pendiente[1] == date(2099, 1, 1)


def _cambio(boe_id: str) -> CambioNormativo:
    return CambioNormativo(
        boe_id=boe_id,
        titulo_norma="Norma de prueba",
        actualizado="20270115T100000Z",
        resumen="Sube la deducción por alquiler.",
        en_vigor_desde="2027-01-01",
        afecta_a=["inquilinos"],
        relevancia="alta",
        cita_literal="la deducción será del 15 por ciento",
        articulos=["Artículo 5"],
    )


def test_a_regional_change_only_reaches_residents_of_that_community(tmp_path: Path) -> None:
    store = NotificationStore(str(tmp_path / "n.db"))
    gallego = make_case("gal", comunidad_autonoma="Galicia", alquiler_vivienda_habitual=6000.0)
    madrileno = make_case("mad", comunidad_autonoma="Madrid", alquiler_vivienda_habitual=6000.0)
    sin_alquiler = make_case("gal2", comunidad_autonoma="Galicia")

    assert notificar_cambio(_cambio(LEY_GALLEGA), [gallego, madrileno, sin_alquiler], store) == 1
    (aviso,) = store.list("gal")
    assert "Resides en Galicia." in aviso.body
    assert "las cifras que usa la calculadora para esta norma se leyeron el" in aviso.body
    assert store.list("mad") == [] and store.list("gal2") == []

    # A change to the state law reaches every tenant, wherever they live.
    assert notificar_cambio(_cambio(LEY_IRPF), [gallego, madrileno, sin_alquiler], store) == 2
    assert len(store.list("mad")) == 1 and "Resides en" not in store.list("mad")[0].body


def test_params_check_reports_and_can_fail(monkeypatch) -> None:
    import hacienda_gpt.cli.boe_watch as boe_watch

    def fake_metadata(boe_id: str, timeout: int = 30) -> dict:
        if boe_id == "BOE-A-2015-945":
            raise RuntimeError("sin conexión")
        return {"fecha_actualizacion": "20990101T000000Z" if boe_id == LEY_GALLEGA else "20200101T000000Z"}

    monkeypatch.setattr(boe_watch, "_fetch_metadata", fake_metadata)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)

    result = CliRunner().invoke(cli_module.cli, ["params-check"])
    assert result.exit_code == 0
    informe = json.loads(result.output)
    assert informe["comprobadas"] == len(normas_de_parametros()) - 1 and len(informe["errores"]) == 1
    (pendiente,) = informe["a_revisar"]
    assert pendiente["boe_id"] == LEY_GALLEGA and pendiente["comunidad"] == "Galicia"
    assert pendiente["actualizada_en_boe_el"] == "2099-01-01"

    assert CliRunner().invoke(cli_module.cli, ["params-check", "--fail-on-stale"]).exit_code == 1
