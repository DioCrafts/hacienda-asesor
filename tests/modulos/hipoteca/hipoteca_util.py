"""Shared helpers for the mortgage module tests (not a conftest: test dirs of several modules share
names; this file name is unique). A small synthetic index series with known values."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hacienda_gpt.modulos.hipoteca.indices import SerieIndices, serie_desde_json

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "hipoteca"
RESOLUCIONES = FIXTURES / "resoluciones"


def serie_sintetica(valores: dict[str, str], indice: str = "euribor_12m") -> SerieIndices:
    """A series whose month M value is published on day 2 of month M+1 under a fake BOE id."""
    series = {}
    for mes, valor in valores.items():
        year, month = map(int, mes.split("-"))
        pub = date(year + (month == 12), month % 12 + 1, 2)
        series[mes] = {
            "valor": valor,
            "literal": valor.replace(".", ","),
            "boe_id": f"BOE-A-{pub.year}-{pub.month:02d}00",
            "publicado": pub.isoformat(),
        }
    return serie_desde_json({"consultado_el": "2026-11-13", "series": {indice: series}})


def serie_basica() -> SerieIndices:
    return serie_sintetica(
        {
            "2022-11": "3.000",
            "2023-11": "2.500",
            "2024-10": "2.900",
            "2024-11": "3.000",
            "2024-12": "2.800",
            "2025-10": "2.000",
            "2025-11": "-0.500",
        }
    )
