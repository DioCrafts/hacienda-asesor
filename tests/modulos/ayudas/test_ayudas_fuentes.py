"""Self-check: every stored figure is spelled in its quote and every quote is verbatim in the law."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path

import pytest

from hacienda_gpt.modulos.ayudas.parametros import (
    DATA_DIR,
    PrestacionesParams,
    cifra_a_decimal,
    comprobar_evidencias,
    ejercicio_para,
    fecha_literal,
    load_prestaciones,
    textos_de_directorio,
)
from .conftest import FIXTURES

EXTRACTOS = FIXTURES / "boe"


def _raw() -> dict[str, object]:
    raw: dict[str, object] = json.loads((DATA_DIR / "prestaciones_2026.json").read_text(encoding="utf-8"))
    return raw


def test_every_figure_is_in_its_quote_and_every_quote_is_in_the_law() -> None:
    p = load_prestaciones(2026)
    textos = textos_de_directorio(EXTRACTOS)
    assert set(textos) == set(p.fuentes_principales)
    assert comprobar_evidencias(p, textos) == []
    assert all(g.fuente.verificado for g in p.grupos().values())


def test_quotes_against_the_full_downloaded_pages_when_available() -> None:
    # Point HACIENDA_BOE_PAGINAS at a directory with BOE-A-*.html straight from boe.es.
    directorio = os.environ.get("HACIENDA_BOE_PAGINAS")
    if not directorio or not (Path(directorio) / "BOE-A-2021-21007.html").exists():
        pytest.skip("HACIENDA_BOE_PAGINAS no contiene la Ley 19/2021")
    textos = {**textos_de_directorio(EXTRACTOS), **textos_de_directorio(Path(directorio))}
    assert comprobar_evidencias(load_prestaciones(2026), textos) == []


def test_the_figures_read_from_the_boe() -> None:
    p = load_prestaciones(2026)
    assert p.iprem.mensual == Decimal(600) and p.iprem.anual_14_pagas == Decimal(8400)
    assert p.imv.pension_no_contributiva_anual == Decimal("8803.20")
    assert p.imv.renta_garantizada_individual == Decimal("733.60")
    assert p.imv.escala(2, 2) == Decimal("1.9") and p.imv.escala(4, 4) == Decimal("2.2")
    assert p.imv.factor_patrimonio(1, 0) == 1 and p.imv.factor_patrimonio(2, 5) == Decimal("2.6")
    assert p.imv.factor_patrimonio(5, 0) == Decimal("2.6")  # "Otros"
    assert (p.hijo_discapacidad.importe_menor, p.hijo_discapacidad.importe_mayor_65) == (
        Decimal(1000),
        Decimal("5962.80"),
    )
    assert p.nacimiento.limite(1, False) == Decimal("15356.00")
    assert p.nacimiento.limite(2, False) == Decimal("17659.40")
    assert p.nacimiento.limite(3, True) == Decimal("23109.00")
    assert p.nacimiento.limite(5, True) == Decimal("23109.00") + 2 * Decimal("3745.00")
    assert (p.bono_social_electrico.descuento_vulnerable, p.bono_social_electrico.descuento_severo) == (
        Decimal("0.425"),
        Decimal("0.575"),
    )
    assert p.bono_social_electrico.descuentos_hasta == date(2026, 12, 31)
    assert p.titulo_monoparental.solicitable_desde == date(2027, 1, 1)
    assert p.titulo_monoparental.fuente.condicionada_a == "BOE-A-2026-23604"


def test_a_wrong_figure_is_caught() -> None:
    raw = _raw()
    raw["imv"]["pension_no_contributiva_anual"] = "8803.00"  # type: ignore[index]
    problemas = comprobar_evidencias(PrestacionesParams.model_validate(raw))
    assert any("pension_no_contributiva_anual" in x for x in problemas)


def test_a_quote_that_is_not_in_the_law_is_caught() -> None:
    raw = _raw()
    raw["capi"]["evidencia"][1]["cita"] = "Menores de tres años: 115 euros."  # type: ignore[index]
    raw["capi"]["evidencia"][1]["cifras"] = {"cuantia_ley_menor_3": "115 euros"}  # type: ignore[index]
    raw["capi"]["cuantia_ley_menor_3"] = "115"  # type: ignore[index]
    problemas = comprobar_evidencias(PrestacionesParams.model_validate(raw), textos_de_directorio(EXTRACTOS))
    assert any("no aparece literal" in x for x in problemas)


def test_a_wrong_date_and_a_figure_without_quote_are_caught() -> None:
    raw = _raw()
    raw["bono_social_electrico"]["descuentos_hasta"] = "2027-06-30"  # type: ignore[index]
    raw["nacimiento"]["evidencia"] = [e for e in raw["nacimiento"]["evidencia"] if "prescripcion_anios" not in e.get("cifras", {})]  # type: ignore[index]
    problemas = comprobar_evidencias(PrestacionesParams.model_validate(raw))
    assert any("descuentos_hasta" in x for x in problemas)
    assert any("prescripcion_anios: dato sin evidencia" in x for x in problemas)


@pytest.mark.parametrize(
    ("literal", "valor"),
    [
        ("8.803,20 euros", Decimal("8803.20")),
        ("42,5 por ciento", Decimal("0.425")),
        ("sesenta y cinco por ciento", Decimal("0.65")),
        ("ciento cincuenta por ciento", Decimal("1.5")),
        ("33%", Decimal("0.33")),
        ("50 %", Decimal("0.5")),
        ("una vez", Decimal(1)),
        ("tres veces", Decimal(3)),
        ("doce", Decimal(12)),
        ("veintiún años", Decimal(21)),
        ("14 pagas", Decimal(14)),
        ("0,3", Decimal("0.3")),
    ],
)
def test_figures_spelled_in_spanish(literal: str, valor: Decimal) -> None:
    assert cifra_a_decimal(literal) == valor


def test_dates_and_year_selection() -> None:
    assert fecha_literal("1 de enero de 2027") == date(2027, 1, 1)
    with pytest.raises(ValueError):
        fecha_literal("31 de febrerillo de 2026")
    assert ejercicio_para(date(2026, 11, 13)) == 2026
    assert ejercicio_para(date(2027, 2, 1)) == 2026  # the latest loaded year until 2027 is read
