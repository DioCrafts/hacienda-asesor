"""Regulated data of the ``luz`` module: figures read from the BOE, quotes verbatim, dated lookups."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from pathlib import Path

import pytest

from hacienda_gpt.modulos.luz.datos import (
    DatoNoDisponibleError,
    Regulados,
    cargar_regulados,
    comprobar_evidencias,
    textos_de_directorio,
)

D = Decimal

EXTRACTOS = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "luz" / "boe"


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    datos = cargar_regulados()
    textos = textos_de_directorio(EXTRACTOS)
    assert set(textos) == set(datos.fuentes_principales)
    assert comprobar_evidencias(datos, textos) == []


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point LUZ_BOE_PAGINAS at a directory with the BOE-*.html pages straight from boe.es.
    directorio = os.environ.get("LUZ_BOE_PAGINAS")
    if not directorio:
        pytest.skip("LUZ_BOE_PAGINAS no está definido")
    assert comprobar_evidencias(cargar_regulados(), textos_de_directorio(Path(directorio))) == []


def _mutar(ruta: list[str | int], valor: object) -> Regulados:
    raw = cargar_regulados().model_dump(mode="json")
    nodo = raw
    for paso in ruta[:-1]:
        nodo = nodo[paso]  # type: ignore[index]
    nodo[ruta[-1]] = valor  # type: ignore[index]
    return Regulados.model_validate(raw)


def test_the_self_check_catches_a_wrong_figure_a_forged_quote_and_a_bad_holiday() -> None:
    textos = textos_de_directorio(EXTRACTOS)
    malo = _mutar(["peajes", 1, "potencia_eur_kw_anio", "p1"], "23.324953")
    assert any("23.324953" in p for p in comprobar_evidencias(malo, textos))
    falsa = _mutar(["cargos", 0, "evidencia", 0, "cita"], "1 3,971618 0,255423 inventado")
    assert any("no aparece literal" in p for p in comprobar_evidencias(falsa, textos))
    festivo = _mutar(["festivos", 0, "fecha"], "2025-02-01")
    problemas = comprobar_evidencias(festivo, textos)
    assert any("bajo el mes" in p for p in problemas)
    horas = _mutar(["periodos_2_0td", "peninsula_baleares_canarias", "p1"], [[9, 14], [18, 22]])
    assert any("franjas" in p for p in comprobar_evidencias(horas, textos))


def test_2025_and_2026_tolls_and_charges_of_2_0td() -> None:
    d = cargar_regulados()
    assert d.peaje(date(2025, 6, 1)).potencia_eur_kw_anio == {"p1": D("22.958932"), "p2": D("0.442165")}
    assert d.peaje(date(2026, 6, 1)).energia_eur_kwh == {"p1": D("0.033261"), "p2": D("0.016409"), "p3": D("0.000077")}
    assert d.cargo(date(2025, 12, 31)).energia_eur_kwh["p1"] == D("0.058305")
    assert d.cargo(date(2026, 1, 1)).potencia_eur_kw_anio["p1"] == D("4.379461")
    with pytest.raises(DatoNoDisponibleError, match="2027"):
        d.peaje(date(2027, 1, 1))


def test_social_bonus_value_changes_in_july_2026() -> None:
    d = cargar_regulados()
    assert d.bono(date(2025, 3, 1)).eur_cups_anio == D("4.650987")
    assert d.bono(date(2026, 6, 30)).eur_cups_anio == D("6.979247")
    assert d.bono(date(2026, 7, 1)).eur_cups_anio == D("9.011295")


def test_tax_timeline_of_2026() -> None:
    d = cargar_regulados()
    assert d.impuestos.iee_general.tipo == D("0.0511269632")
    assert d.impuestos.iva_general.tipo == D("0.21")
    assert d.tramo_fiscal(date(2026, 3, 21)) is None
    rebaja = d.tramo_fiscal(date(2026, 3, 22))
    assert rebaja is not None and (rebaja.estado, rebaja.iva_tipo, rebaja.iee_tipo) == (
        "aplicada",
        D("0.10"),
        D("0.005"),
    )
    assert d.tramo_fiscal(date(2026, 5, 31)).estado == "aplicada"  # type: ignore[union-attr]
    assert d.tramo_fiscal(date(2026, 6, 15)).estado == "no_aplicada"  # type: ignore[union-attr]
    assert d.tramo_fiscal(date(2026, 7, 15)) is None
    assert d.tramo_fiscal(date(2026, 8, 15)).estado == "condicional"  # type: ignore[union-attr]
    assert d.tramo_fiscal(date(2026, 9, 15)).estado == "no_aplicada"  # type: ignore[union-attr]
    assert d.tramo_fiscal(date(2026, 10, 15)) is None
    assert d.tramo_fiscal(date(2026, 11, 15)).estado == "condicional"  # type: ignore[union-attr]
    assert d.tramo_fiscal(date(2025, 8, 15)) is None


def test_every_source_has_a_boe_url_and_hash() -> None:
    for boe_id, fuente in cargar_regulados().fuentes_principales.items():
        assert fuente.boe_id == boe_id
        assert fuente.url.startswith("https://www.boe.es/") and boe_id in fuente.url
        assert len(fuente.sha256_html) == 64
