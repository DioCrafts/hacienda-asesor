"""Document ingestion and chunking — Docling-only pipeline.

Docling is the single ingestion backend. It parses PDF and HTML with
layout-, reading-order- and table-aware models, and its tokenization-aware
``HybridChunker`` splits each document along its own structure (title →
section → element) while respecting the embedder's token budget. The chunker
keeps tables intact (``repeat_table_header``) instead of flattening them into
a stream of numbers.

This module deliberately carries **no fiscal-domain metadata inference**: the
chunk metadata exposed downstream comes straight from Docling itself (heading
hierarchy, originating filename, page number). Those are exactly the fields the
grounding gate needs to decide whether a chunk is citable.

Heavy imports (``docling`` / ``langchain_docling`` / FAISS) are deferred so
that importing this module never drags in the ML stack — unit tests stub the
loader seam instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

# File types Docling ingests for this corpus. PDF unlocks the BOE consolidated
# texts and AEAT manuals/folletos that the legacy HTML-only indexer ignored.
SUPPORTED_SUFFIXES: tuple[str, ...] = (".pdf", ".html", ".htm")


def _clean_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_dl_meta(raw: Any) -> dict[str, Any]:
    """Coerce langchain-docling's ``dl_meta`` into a plain dict.

    langchain-docling stores it as a JSON-serializable dict, but be defensive
    about pydantic-model variants across versions.
    """
    if isinstance(raw, dict):
        return raw
    for attr in ("model_dump", "export_json_dict"):
        method = getattr(raw, attr, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:  # noqa: BLE001 - best-effort metadata read
                return {}
            return dumped if isinstance(dumped, dict) else {}
    return {}


def _headings_title(dl_meta: dict[str, Any]) -> str | None:
    headings = dl_meta.get("headings")
    if not isinstance(headings, list):
        return None
    cleaned = [h.strip() for h in headings if isinstance(h, str) and h.strip()]
    # Join the heading hierarchy (e.g. "Título III — Artículo 96") so the
    # citation title is descriptive rather than just the deepest heading.
    return " — ".join(cleaned) if cleaned else None


def _origin_filename(dl_meta: dict[str, Any]) -> str | None:
    origin = dl_meta.get("origin")
    if isinstance(origin, dict):
        return _clean_text(origin.get("filename"))
    return None


def _first_page_no(dl_meta: dict[str, Any]) -> int | None:
    for item in dl_meta.get("doc_items") or []:
        if not isinstance(item, dict):
            continue
        for prov in item.get("prov") or []:
            page = prov.get("page_no") if isinstance(prov, dict) else None
            if isinstance(page, int):
                return page
    return None


def docling_chunk_metadata(doc: Document) -> dict[str, Any]:
    """Flatten Docling's native chunk metadata into the citation-facing keys.

    Surfaces only what Docling produced — heading hierarchy (``title`` /
    ``section``), the source locator (``source_url``) and the originating page
    (``page_no``). The grounding gate marks a chunk citable when it carries a
    ``title`` and a locator, so those are guaranteed non-empty.
    """
    metadata = dict(doc.metadata or {})
    dl_meta = _as_dl_meta(metadata.get("dl_meta"))

    source = _clean_text(metadata.get("source")) or _origin_filename(dl_meta)
    heading = _headings_title(dl_meta)
    title = heading or _origin_filename(dl_meta) or source or "sin_titulo"

    metadata["source_url"] = source or ""
    metadata["title"] = title
    metadata["section"] = heading or "general"
    page_no = _first_page_no(dl_meta)
    if page_no is not None:
        metadata["page_no"] = page_no
    return metadata


class DocumentProcessor:
    """Build a FAISS index from a content directory using Docling."""

    def __init__(
        self,
        embeddings: Embeddings,
        content_dir: str,
        output_dir: str,
        *,
        max_tokens: int | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.content_dir = content_dir
        self.output_dir = output_dir
        self.max_tokens = max_tokens

    def discover_files(self) -> list[str]:
        root = Path(self.content_dir)
        if not root.exists():
            return []
        return [str(path) for path in sorted(root.rglob("*")) if path.suffix.lower() in SUPPORTED_SUFFIXES]

    def _build_loader(self, files: list[str]) -> Any:
        """Construct the DoclingLoader. Isolated so tests can stub the seam."""
        from docling.chunking import HybridChunker
        from langchain_docling.loader import DoclingLoader, ExportType

        from hacienda_gpt import settings

        chunker_kwargs: dict[str, Any] = {"tokenizer": settings.EMBEDDING_MODEL}
        if self.max_tokens:
            chunker_kwargs["max_tokens"] = self.max_tokens
        return DoclingLoader(
            file_path=files,
            export_type=ExportType.DOC_CHUNKS,
            chunker=HybridChunker(**chunker_kwargs),
        )

    def load_chunks(self) -> list[Document]:
        files = self.discover_files()
        if not files:
            logging.warning("No %s files found under %s", SUPPORTED_SUFFIXES, self.content_dir)
            return []
        logging.info("Converting & chunking %d files with Docling", len(files))
        raw_chunks = self._build_loader(files).load()
        return [
            Document(page_content=chunk.page_content, metadata=docling_chunk_metadata(chunk)) for chunk in raw_chunks
        ]

    def process_documents(self) -> None:
        from langchain_community.vectorstores import FAISS

        logging.info("Loading documents from %s", self.content_dir)
        chunks = self.load_chunks()
        logging.info("Produced %d Docling chunks for indexing", len(chunks))
        if not chunks:
            raise RuntimeError(f"No indexable chunks produced from {self.content_dir!r}; nothing to index.")
        db = FAISS.from_documents(chunks, self.embeddings)
        db.save_local(self.output_dir)
        logging.info("Local FAISS index successfully saved to %s", self.output_dir)


def build_index(args: dict) -> None:
    """Build and persist the FAISS index using the configured local embedder."""
    # Lazy import keeps the processor module (and its CLI) light, and routes
    # through the shared embedder factory so the index always matches the
    # query path.
    from hacienda_gpt.llm.embeddings import create_embeddings

    processor = DocumentProcessor(create_embeddings(), **args)
    processor.process_documents()
