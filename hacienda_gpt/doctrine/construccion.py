"""Build the doctrine graph from the indexed corpus."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from hacienda_gpt.doctrine.citas import RANGO, Documento, extract_citations, identify_document
from hacienda_gpt.doctrine.grafo import Arista, DoctrineGraph
from hacienda_gpt.doctrine.relaciones import Lectura, leer_heuristica, leer_llm

logger = logging.getLogger(__name__)


@dataclass
class BuildReport:
    documentos: int = 0
    documentos_sin_identidad: int = 0
    citas: int = 0
    relaciones: dict[str, int] = field(default_factory=dict)
    llamadas_llm: int = 0
    lecturas_llm_descartadas: int = 0


def _group_by_document(chunks: Iterable[Any]) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """Reassemble documents from their chunks (keyed by source file)."""
    docs: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for chunk in chunks:
        metadata = dict(getattr(chunk, "metadata", None) or {})
        key = str(metadata.get("source_file") or metadata.get("source_url") or metadata.get("source") or "")
        if not key:
            continue
        docs.setdefault(key, (metadata, []))[1].append(getattr(chunk, "page_content", "") or "")
    return docs


def _worth_llm(origen: Documento, cita_tipo: str, lectura: Lectura) -> bool:
    """Spend an LLM call only where a wrong reading would change the answer:
    a court or the TEAC citing doctrine of equal or lower rank, or any citation
    the heuristic already flagged as more than a neutral mention."""
    if lectura.relacion != "cita":
        return True
    return origen.tipo in ("TS", "TC", "TEAC") and RANGO.get(origen.tipo, 0) >= RANGO.get(cita_tipo, 0)


def build_graph(
    chunks: Iterable[Any],
    graph: DoctrineGraph,
    *,
    provider: Any | None = None,
    max_llm_calls: int = 0,
) -> BuildReport:
    """Add every doctrine document and its citations to ``graph``.

    Idempotent: re-running over the same corpus updates nodes and never
    downgrades an edge. With ``provider`` and ``max_llm_calls > 0`` the
    citations that matter are re-read by the LLM, up to that many calls.
    """
    report = BuildReport()
    documents = _group_by_document(chunks)

    # Pass 1: every corpus document becomes a node, so that citations between
    # corpus documents resolve to the same node whatever key they use.
    identities: dict[str, Documento] = {}
    for key, (metadata, texts) in documents.items():
        doc = identify_document(metadata, " ".join(texts[:2]))
        if doc is None:
            report.documentos_sin_identidad += 1
            continue
        doc.clave = graph.upsert(doc, en_corpus=True)
        identities[key] = doc
        report.documentos += 1

    # Pass 2: citations.
    for key, doc in identities.items():
        text = "\n".join(documents[key][1])
        for cita in extract_citations(text, propias={doc.clave, *doc.alias}):
            report.citas += 1
            destino = graph.upsert(
                Documento(cita.clave, cita.tipo, alias=list(cita.alias), fecha=cita.fecha), en_corpus=False
            )
            if destino == doc.clave:
                continue
            # Explicit wording counts only in the sentence that names the cited document.
            lectura = leer_heuristica(cita.frase or cita.contexto)
            if provider is not None and report.llamadas_llm < max_llm_calls and _worth_llm(doc, cita.tipo, lectura):
                report.llamadas_llm += 1
                nodo = graph.get(destino)
                refined = leer_llm(
                    provider,
                    origen=f"{doc.titulo or doc.clave} ({doc.tipo})",
                    destino=graph.etiqueta(nodo) if nodo else cita.texto,
                    contexto=cita.contexto,
                )
                if refined is None:
                    report.lecturas_llm_descartadas += 1
                else:
                    lectura = refined
            graph.add_edge(
                Arista(
                    origen=doc.clave,
                    destino=destino,
                    relacion=lectura.relacion,
                    confianza=lectura.confianza,
                    evidencia=lectura.evidencia or cita.texto,
                    metodo=lectura.metodo,
                )
            )
            report.relaciones[lectura.relacion] = report.relaciones.get(lectura.relacion, 0) + 1
    return report
