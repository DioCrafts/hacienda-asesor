"""LLM spend report::

    uv run python -m hacienda_gpt.cli.consumo resumen --dias 7

See ``docs/operacion.md``.
"""

from __future__ import annotations

from datetime import timedelta
import json

import click

from hacienda_gpt import settings
from hacienda_gpt.ops.consumo import ConsumoStore, hoy_madrid


@click.group()
def cli() -> None:
    """Consumo de modelos de IA (tokens y coste estimado)."""


@cli.command("resumen")
@click.option(
    "--dias",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Días naturales (hora peninsular) que abarca el informe, contando hoy.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Base de datos SQLite. Por defecto, la de los casos (DECISION_STATE_DB_PATH).",
)
def resumen(dias: int, db_path: str | None) -> None:
    """Imprime en JSON el consumo total y por día, propósito, usuario y caso."""
    store = ConsumoStore(db_path or settings.DECISION_STATE_DB_PATH)
    try:
        hoy = hoy_madrid()
        informe = store.resumen(desde=hoy - timedelta(days=dias - 1), hasta=hoy)
        salida = informe.model_dump(mode="json")
        salida["precios_consultados_el"] = store.precios().consultado_el.isoformat()
    finally:
        store.close()
    click.echo(json.dumps(salida, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
