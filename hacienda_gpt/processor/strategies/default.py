"""Default Docling-backed strategy — claims every file as a last resort.

This is the strategy the project shipped before the registry existed:
parse with :class:`docling.document_converter.DocumentConverter` via
``langchain_docling.DoclingLoader`` and chunk with the per-worker
:class:`docling.chunking.HybridChunker`. It is the right call for the
overwhelming majority of files in the corpus — small to medium HTMLs /
PDFs where Docling's layout-aware parsing and tokenizer-aware chunking
both pay off.

It is registered **last** in the registry so it catches anything no
specialised strategy claimed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.documents import Document

from hacienda_gpt.processor.strategies.base import IngestStrategy


class DefaultDoclingStrategy(IngestStrategy):
    """Catch-all strategy: Docling parse + per-worker HybridChunker.

    Lives one level removed from ``_process_one_file`` in
    ``document_loader.py`` so we can route to a different strategy without
    touching the loader. The actual Docling call is implemented in the
    loader (it owns the per-worker tokenizer state); this strategy is
    intentionally a thin shim that signals **"use the default path"**.
    """

    strategy_id = "default-docling-v1"

    def probe(self, file_path: str) -> bool:  # noqa: ARG002 — interface contract
        # Always wins as the last-registered strategy. Returning True
        # unconditionally is the explicit "I claim everything that fell
        # through" — registration order guarantees no specialised
        # strategy is shadowed.
        return True

    def parse(self, file_path: str) -> Sequence[Document]:
        # Concrete parsing lives in ``document_loader._docling_parse_default``
        # because it needs the per-worker ``_WORKER_CHUNKER`` initialised by
        # the multiprocessing pool initialiser. Importing it here avoids a
        # circular import at module load.
        from hacienda_gpt.processor.document_loader import _docling_parse_default

        return _docling_parse_default(file_path)


def parse_with_default(file_path: str) -> Sequence[Document]:
    """Module-level convenience for callers that don't want to instantiate.

    Useful for strategies that delegate the **chunking** step to the
    default Docling path after doing their own pre-processing (e.g. the
    BOE strategy splits a giant HTML into smaller chunks then runs the
    default chunker on each).
    """
    return DefaultDoclingStrategy().parse(file_path)


def docling_parse_files(file_paths: list[str]) -> list[Document]:
    """Run Docling on a list of files in one go, return all chunks.

    Thin wrapper around the loader's batched-Docling helper so strategies
    can ask "parse these N pre-split fragments" without re-implementing
    the chunker initialisation. Used by ``BoeConsolidadoTableStrategy``
    after it has produced the temp sub-files.
    """
    from hacienda_gpt.processor.document_loader import _docling_parse_paths

    return _docling_parse_paths(file_paths)


# Internal helper signature surfaced for strategies that want it without
# importing from ``document_loader`` directly — keeps the dependency
# direction strategies → loader rather than the reverse.
def _stable_chunk_id_for(file_path: str, idx: int) -> str:
    from hacienda_gpt.processor.document_loader import _stable_chunk_id

    return _stable_chunk_id(file_path, idx)


__all__: list[Any] = ["DefaultDoclingStrategy", "parse_with_default", "docling_parse_files"]
