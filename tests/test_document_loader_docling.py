"""Docling ingestion: metadata mapping, file discovery and chunk loading.

The loader carries no fiscal-domain inference anymore; these tests pin the
mapping from Docling's native chunk metadata (heading hierarchy, source,
page number) onto the citation-facing keys the grounding gate reads.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

from hacienda_gpt.processor.document_loader import (
    SUPPORTED_SUFFIXES,
    DocumentProcessor,
    docling_chunk_metadata,
)


class _DummyEmbeddings:
    def embed_documents(self, texts):
        return [[0.0] * 3 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 3


def _processor(content_dir: str = ".") -> DocumentProcessor:
    return DocumentProcessor(embeddings=_DummyEmbeddings(), content_dir=content_dir, output_dir=".")


def test_docling_metadata_maps_headings_source_and_page() -> None:
    chunk = Document(
        page_content="Artículo 96. Obligación de declarar...",
        metadata={
            "source": "/data/boe/BOE-A-2006-20764.pdf",
            "dl_meta": {
                "headings": ["Título III", "Artículo 96"],
                "origin": {"filename": "BOE-A-2006-20764.pdf"},
                "doc_items": [{"prov": [{"page_no": 12}]}],
            },
        },
    )
    meta = docling_chunk_metadata(chunk)
    assert meta["title"] == "Título III — Artículo 96"
    assert meta["section"] == "Título III — Artículo 96"
    assert meta["source_url"] == "/data/boe/BOE-A-2006-20764.pdf"
    assert meta["page_no"] == 12


def test_docling_metadata_falls_back_to_filename_without_headings() -> None:
    chunk = Document(
        page_content="texto",
        metadata={"source": "", "dl_meta": {"origin": {"filename": "manual-renta.pdf"}}},
    )
    meta = docling_chunk_metadata(chunk)
    assert meta["title"] == "manual-renta.pdf"
    assert meta["source_url"] == "manual-renta.pdf"
    assert meta["section"] == "general"
    assert "page_no" not in meta


def test_docling_metadata_always_has_title_and_locator_keys() -> None:
    # title + locator must always be present so the grounding gate can decide
    # citability deterministically, even on a degenerate empty chunk.
    meta = docling_chunk_metadata(Document(page_content="x", metadata={}))
    assert meta["title"] == "sin_titulo"
    assert meta["source_url"] == ""
    assert meta["section"] == "general"


def test_discover_files_selects_supported_types(tmp_path: Path) -> None:
    (tmp_path / "a.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.htm").write_text("x", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ignore.png").write_bytes(b"\x89PNG")

    names = sorted(Path(f).name for f in _processor(str(tmp_path)).discover_files())
    assert names == ["a.html", "b.pdf", "c.htm"]
    assert {Path(n).suffix for n in names} <= set(SUPPORTED_SUFFIXES)


def test_load_chunks_maps_loader_output(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.7 fake")

    class _FakeLoader:
        def load(self):
            return [
                Document(
                    page_content="Tipo de IVA general 21%.",
                    metadata={"source": str(tmp_path / "doc.pdf"), "dl_meta": {"headings": ["IVA"]}},
                )
            ]

    processor = _processor(str(tmp_path))
    monkeypatch.setattr(processor, "_build_loader", lambda files: _FakeLoader())

    chunks = processor.load_chunks()
    assert len(chunks) == 1
    assert chunks[0].metadata["title"] == "IVA"
    assert chunks[0].metadata["source_url"].endswith("doc.pdf")
    assert chunks[0].page_content.startswith("Tipo de IVA")


def test_load_chunks_empty_when_no_files(tmp_path: Path) -> None:
    assert _processor(str(tmp_path)).load_chunks() == []
