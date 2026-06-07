from __future__ import annotations

import json
from pathlib import Path

from langchain_core.documents import Document

from hacienda_gpt.processor.document_loader import (
    _load_sidecar,
    _merge_sidecar,
    _process_one_file,
)


# --------------------------------------------------------------------- #
# _load_sidecar — locating the crawler JSON next to a source file
# --------------------------------------------------------------------- #
def test_load_sidecar_same_basename(tmp_path: Path) -> None:
    html = tmp_path / "criterio_10.html"
    html.write_text("contenido", encoding="utf-8")
    (tmp_path / "criterio_10.json").write_text(
        json.dumps({"source_authority": "TEAC", "vinculante": True}), encoding="utf-8"
    )
    assert _load_sidecar(str(html)) == {"source_authority": "TEAC", "vinculante": True}


def test_load_sidecar_per_directory_metadata(tmp_path: Path) -> None:
    ccaa_dir = tmp_path / "andalucia"
    ccaa_dir.mkdir()
    pdf = ccaa_dir / "boja.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (ccaa_dir / "metadata.json").write_text(
        json.dumps({"jurisdiction_scope": "autonomico:AN"}), encoding="utf-8"
    )
    assert _load_sidecar(str(pdf)) == {"jurisdiction_scope": "autonomico:AN"}


def test_load_sidecar_prefers_same_basename_over_metadata(tmp_path: Path) -> None:
    doc_dir = tmp_path / "x"
    doc_dir.mkdir()
    html = doc_dir / "doc.html"
    html.write_text("contenido", encoding="utf-8")
    (doc_dir / "doc.json").write_text(json.dumps({"k": "same"}), encoding="utf-8")
    (doc_dir / "metadata.json").write_text(json.dumps({"k": "dir"}), encoding="utf-8")
    assert _load_sidecar(str(html)) == {"k": "same"}


def test_load_sidecar_missing_returns_empty(tmp_path: Path) -> None:
    html = tmp_path / "doc.html"
    html.write_text("contenido", encoding="utf-8")
    assert _load_sidecar(str(html)) == {}


def test_load_sidecar_malformed_returns_empty(tmp_path: Path) -> None:
    html = tmp_path / "doc.html"
    html.write_text("contenido", encoding="utf-8")
    (tmp_path / "doc.json").write_text("{ not valid json", encoding="utf-8")
    assert _load_sidecar(str(html)) == {}


# --------------------------------------------------------------------- #
# _merge_sidecar — additive merge that never clobbers citation keys
# --------------------------------------------------------------------- #
def test_merge_sidecar_protects_reserved_and_skips_empty() -> None:
    meta = {"title": "Docling Title", "source_url": "/path/doc.html"}
    _merge_sidecar(
        meta,
        {
            "title": "Sidecar Title",          # reserved -> not overwritten
            "source_url": "https://boe.es/x",  # reserved -> not overwritten
            "source_authority": "TS",          # merged
            "covered_tributos": ["IRPF", "IVA"],  # merged (list)
            "vinculante": False,               # merged (False is meaningful)
            "concepto": "",                    # empty -> skipped
            "ponente": None,                   # None -> skipped
        },
    )
    assert meta["title"] == "Docling Title"
    assert meta["source_url"] == "/path/doc.html"
    assert meta["source_authority"] == "TS"
    assert meta["covered_tributos"] == ["IRPF", "IVA"]
    assert meta["vinculante"] is False
    assert "concepto" not in meta
    assert "ponente" not in meta


# --------------------------------------------------------------------- #
# Integration — _process_one_file merges the sidecar onto every chunk
# --------------------------------------------------------------------- #
def test_process_one_file_merges_sidecar(tmp_path: Path) -> None:
    from hacienda_gpt.processor import strategies

    doc_path = tmp_path / "criterio_10.html"
    doc_path.write_text("contenido", encoding="utf-8")
    (tmp_path / "criterio_10.json").write_text(
        json.dumps(
            {"source_authority": "TEAC", "is_tributario": True, "covered_tributos": ["IVA"]}
        ),
        encoding="utf-8",
    )

    class _FakeStrategy(strategies.IngestStrategy):
        strategy_id = "sidecar-test-fake-v1"

        def probe(self, file_path: str) -> bool:
            return True

        def parse(self, file_path: str):
            # No Docling: a citation-ready chunk straight from the strategy.
            return [
                Document(
                    page_content="c",
                    metadata={"title": "T", "source_url": file_path, "section": "general"},
                )
            ]

    strategies.reset_registry()
    strategies.register(_FakeStrategy())
    try:
        returned_path, docs = _process_one_file(str(doc_path))
    finally:
        # Restore the standard registry for any test that runs after.
        strategies.reset_registry()
        strategies.install_default_strategies()

    assert returned_path == str(doc_path)
    assert len(docs) == 1
    md = docs[0].metadata
    # Sidecar metadata is now on the chunk…
    assert md["source_authority"] == "TEAC"
    assert md["is_tributario"] is True
    assert md["covered_tributos"] == ["IVA"]
    # …and the citation/contract keys are untouched.
    assert md["title"] == "T"
    assert md["source_file"] == str(doc_path)
