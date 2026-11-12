"""Build and inspect the doctrine graph.

# Build (or update) the graph from the indexed corpus — free, no LLM:
uv run python -m hacienda_gpt.cli.doctrine_graph build

# Re-read the citations that matter with the LLM (asks before spending):
uv run python -m hacienda_gpt.cli.doctrine_graph build --llm --max-llm-calls 500

# What the graph holds, and which Supreme Court rulings the corpus cites but lacks:
uv run python -m hacienda_gpt.cli.doctrine_graph stats
uv run python -m hacienda_gpt.cli.doctrine_graph wanted --limit 50 --out reports/sentencias_a_conseguir.json

# Validity of one document:
uv run python -m hacienda_gpt.cli.doctrine_graph status "V1234-19"
"""

from __future__ import annotations

import json
from pathlib import Path
import pickle
from typing import Any

import click

from hacienda_gpt import settings
from hacienda_gpt.doctrine.citas import extract_citations
from hacienda_gpt.doctrine.construccion import build_graph
from hacienda_gpt.doctrine.grafo import DoctrineGraph


def load_corpus_chunks(index_path: str) -> list[Any]:
    """Chunks of the FAISS index, read from its docstore without loading the
    embedder (building the graph needs the text, not the vectors)."""
    if not settings.FAISS_TRUSTED_INDEX:
        raise click.ClickException(
            "El índice FAISS se lee con pickle: exporta FAISS_TRUSTED_INDEX=true solo si el índice es tuyo y de confianza."
        )
    path = Path(index_path) / "index.pkl"
    if not path.exists():
        raise click.ClickException(f"No existe {path}. Construye antes el índice con hacienda_gpt.cli.processor.")
    with path.open("rb") as handle:
        docstore, _ = pickle.load(handle)  # noqa: S301 - gated by FAISS_TRUSTED_INDEX above
    return list(docstore._dict.values())


@click.group()
def cli() -> None:
    """Grafo de vigencia de la doctrina tributaria."""


_GRAPH_OPTION = click.option("--graph", "graph_path", default=settings.DOCTRINE_GRAPH_PATH, show_default=True)


@cli.command()
@click.option("--index-path", default=settings.FAISS_INDEX_PATH, show_default=True, help="Directorio del índice FAISS.")
@_GRAPH_OPTION
@click.option("--llm/--no-llm", default=False, help="Releer con el LLM las citas relevantes (de pago).")
@click.option("--max-llm-calls", default=300, show_default=True)
@click.option("--yes", is_flag=True, help="No pedir confirmación antes de llamar al LLM.")
def build(index_path: str, graph_path: str, llm: bool, max_llm_calls: int, yes: bool) -> None:
    """Construye o actualiza el grafo a partir del corpus indexado."""
    chunks = load_corpus_chunks(index_path)
    provider = None
    if llm:
        from hacienda_gpt.agent.providers import create_provider

        provider = create_provider(effort=settings.DECISION_EXTRACTOR_EFFORT)
        if not yes:
            click.confirm(
                f"Se harán hasta {max_llm_calls} llamadas a {provider.name}/{provider.model}. ¿Continuar?", abort=True
            )
    graph = DoctrineGraph(graph_path)
    report = build_graph(chunks, graph, provider=provider, max_llm_calls=max_llm_calls if llm else 0)
    click.echo(
        json.dumps(
            {
                "fragmentos_leidos": len(chunks),
                "documentos_de_doctrina": report.documentos,
                "documentos_sin_identidad": report.documentos_sin_identidad,
                "citas": report.citas,
                "relaciones": report.relaciones,
                "llamadas_llm": report.llamadas_llm,
                "lecturas_llm_descartadas": report.lecturas_llm_descartadas,
                "grafo": graph.stats(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    click.echo(f"Grafo guardado en {graph_path}")


@cli.command()
@_GRAPH_OPTION
def stats(graph_path: str) -> None:
    """Resumen del grafo."""
    click.echo(json.dumps(DoctrineGraph(graph_path).stats(), indent=2, ensure_ascii=False))


@cli.command()
@_GRAPH_OPTION
@click.option("--tipo", type=click.Choice(["TS", "TC", "TEAC", "DGT"]), default="TS", show_default=True)
@click.option("--limit", default=50, show_default=True)
@click.option("--out", "out_path", default=None, type=click.Path(dir_okay=False, path_type=Path))
def wanted(graph_path: str, tipo: str, limit: int, out_path: Path | None) -> None:
    """Resoluciones que el corpus cita pero no contiene, de más a menos citadas.

    Es la lista de adquisición: las sentencias en las que la DGT y el TEAC se
    apoyan de verdad, que son las que merece la pena conseguir una a una.
    """
    graph = DoctrineGraph(graph_path)
    rows = [
        {
            "referencia": graph.etiqueta(nodo),
            "clave": nodo.clave,
            "fecha": nodo.fecha.isoformat() if nodo.fecha else None,
            "veces_citada": count,
        }
        for nodo, count in graph.mas_citadas_fuera_del_corpus(tipo, limit)
    ]
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        click.echo(f"{len(rows)} referencias → {out_path}")
        return
    for row in rows:
        click.echo(f"{row['veces_citada']:>4}  {row['referencia']}")


@cli.command()
@_GRAPH_OPTION
@click.argument("referencia")
def status(graph_path: str, referencia: str) -> None:
    """Vigencia de una resolución, tal como se cita (p. ej. "V1234-19")."""
    graph = DoctrineGraph(graph_path)
    citas = extract_citations(referencia)
    if not citas:
        raise click.ClickException("No reconozco esa referencia.")
    for cita in citas:
        status_ = graph.vigencia(cita.clave)
        click.echo(f"{cita.clave}: {status_.estado} — {status_.resumen}")
        for arista in status_.motivos:
            click.echo(
                f"    {arista.origen} {arista.relacion} ({arista.metodo}, {arista.confianza:.2f}): {arista.evidencia}"
            )


if __name__ == "__main__":
    cli()
