"""End-to-end coverage of the incremental indexing flow.

Stubs ``_run_docling_on`` so we never need the heavy Docling stack, but
exercises the **real** FAISS code path (langchain-community 0.4.1) so the
add/delete semantics that the plan depends on are validated against the
actual library, not our wishful thinking.

Each test is a tiny end-to-end scenario from the section "4.2 Tests de
integración" of ``docs/incremental_indexing_plan.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from hacienda_gpt.processor import document_loader as dl
from hacienda_gpt.processor import manifest as mf


class _DeterministicEmbed(Embeddings):
    """Tiny embedder that returns a stable vector per text.

    FAISS only checks that all vectors have the same dimensionality and that
    we can compute one for an arbitrary query. The content of the vector
    doesn't matter for the incremental flow (add/delete by id), so we keep
    it deterministic and dimension-stable.
    """

    DIM = 8

    def _vec(self, text: str) -> list[float]:
        return [(hash(text) % 97) / 97.0 + 0.001 * i for i in range(self.DIM)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Filesystem layout: a small ``content_dir`` plus a fresh ``output_dir``
    that the processor will populate."""
    content_dir = tmp_path / "html"
    content_dir.mkdir()
    output_dir = tmp_path / "faiss"
    return content_dir, output_dir


def _write(path: Path, content: str = "<html>x</html>") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_processor(content_dir: Path, output_dir: Path, *, incremental: bool = True) -> dl.DocumentProcessor:
    return dl.DocumentProcessor(
        embeddings=_DeterministicEmbed(),
        content_dir=str(content_dir),
        output_dir=str(output_dir),
        max_tokens=512,
        num_workers=1,
        incremental=incremental,
        embedder_model="test/embedder",
        embedder_dim=8,
    )


def _docs_for(file_path: str, n: int) -> list[Document]:
    """Stub Docling output: n chunks with stable ids derived from the path."""
    return [
        Document(
            page_content=f"chunk {i} of {Path(file_path).name}",
            metadata={
                "chunk_id": dl._stable_chunk_id(file_path, i),
                "source_file": file_path,
                "title": Path(file_path).stem,
                "source_url": file_path,
                "section": "general",
            },
        )
        for i in range(n)
    ]


@pytest.fixture
def patch_docling(monkeypatch: pytest.MonkeyPatch):
    """Replace ``_run_docling_on`` with a predictable stub keyed on filename.

    The stub yields 2 chunks per .html file by default; tests can override
    the per-file chunk count via the returned ``override`` dict.
    """
    override: dict[str, int] = {}

    def fake_run(self, files: list[str]) -> list[Document]:
        docs: list[Document] = []
        for f in files:
            count = override.get(Path(f).name, 2)
            docs.extend(_docs_for(f, count))
        return docs

    monkeypatch.setattr(dl.DocumentProcessor, "_run_docling_on", fake_run)
    return override


# --------------------------------------------------------------------------- #
# 1. First run — no manifest yet
# --------------------------------------------------------------------------- #


def test_first_run_builds_index_and_manifest(corpus, patch_docling) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _write(content_dir / "b.html")

    _make_processor(content_dir, output_dir).process_documents()

    assert (output_dir / "index.faiss").exists()
    assert (output_dir / "index.pkl").exists()
    assert (output_dir / mf.MANIFEST_FILENAME).exists()

    manifest = mf.load_manifest(output_dir)
    assert manifest is not None
    assert set(manifest.files.keys()) == {"a.html", "b.html"}
    # Each file got 2 chunks (the stub default) with stable ids.
    for entry in manifest.files.values():
        assert entry.n_chunks == 2
        assert len(entry.chunk_ids) == 2


# --------------------------------------------------------------------------- #
# 2. Second run, no changes
# --------------------------------------------------------------------------- #


def test_second_run_without_changes_does_no_docling_work(corpus, patch_docling, monkeypatch) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _write(content_dir / "b.html")

    _make_processor(content_dir, output_dir).process_documents()

    # Second run: instrument _run_docling_on to fail if invoked at all.
    called: dict[str, list] = {"args": []}
    original = dl.DocumentProcessor._run_docling_on

    def spy(self, files):
        called["args"].append(list(files))
        return original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_run_docling_on", spy)

    _make_processor(content_dir, output_dir).process_documents()

    # The diff is "all unchanged" → no Docling work at all.
    assert called["args"] == [] or all(len(a) == 0 for a in called["args"])


# --------------------------------------------------------------------------- #
# 3. New file added
# --------------------------------------------------------------------------- #


def test_added_file_processes_only_the_new_one(corpus, patch_docling, monkeypatch) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _make_processor(content_dir, output_dir).process_documents()

    _write(content_dir / "b.html")  # new

    docling_calls: list[list[str]] = []
    original = dl.DocumentProcessor._run_docling_on

    def spy(self, files):
        docling_calls.append([Path(f).name for f in files])
        return original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_run_docling_on", spy)
    _make_processor(content_dir, output_dir).process_documents()

    # Only b.html should have been re-processed.
    assert docling_calls and docling_calls[-1] == ["b.html"]

    manifest = mf.load_manifest(output_dir)
    assert set(manifest.files.keys()) == {"a.html", "b.html"}


# --------------------------------------------------------------------------- #
# 4. File modified — old chunks evicted, new chunks added
# --------------------------------------------------------------------------- #


def test_modified_file_evicts_old_chunks_and_adds_new(corpus, patch_docling, monkeypatch) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html", "<html>v1</html>")
    _make_processor(content_dir, output_dir).process_documents()

    manifest_before = mf.load_manifest(output_dir)
    old_chunk_ids = manifest_before.files["a.html"].chunk_ids

    # Mutate the file and bump the stub chunk count so we can tell apart
    # the new chunks from the old in the resulting index.
    _write(content_dir / "a.html", "<html>v2 different content</html>")
    patch_docling["a.html"] = 3  # produce 3 chunks this time

    from langchain_community.vectorstores import FAISS

    delete_calls: list[list[str]] = []
    real_delete = FAISS.delete

    def spy_delete(self, ids=None, **kwargs):
        delete_calls.append(list(ids or []))
        return real_delete(self, ids=ids, **kwargs)

    monkeypatch.setattr(FAISS, "delete", spy_delete)

    _make_processor(content_dir, output_dir).process_documents()

    # FAISS.delete must have been called with exactly the manifest's old ids.
    assert delete_calls and set(delete_calls[0]) == set(old_chunk_ids)

    # Manifest reflects the new chunk count.
    manifest_after = mf.load_manifest(output_dir)
    entry = manifest_after.files["a.html"]
    assert entry.n_chunks == 3
    # The new chunk_ids are deterministic from the path + ordinal, so they
    # don't depend on content — but they DO differ from the old ones only
    # because old ones were for a single chunk pair (idx 0-1) while new
    # ones span 0-2. The first two ids overlap by design; the third is new.
    assert len(entry.chunk_ids) == 3


# --------------------------------------------------------------------------- #
# 5. File removed from disk — chunks evicted
# --------------------------------------------------------------------------- #


def test_removed_file_evicts_chunks(corpus, patch_docling, monkeypatch) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _write(content_dir / "b.html")
    _make_processor(content_dir, output_dir).process_documents()

    manifest_before = mf.load_manifest(output_dir)
    b_ids = manifest_before.files["b.html"].chunk_ids

    (content_dir / "b.html").unlink()

    from langchain_community.vectorstores import FAISS

    deleted: list[list[str]] = []
    real_delete = FAISS.delete

    def spy_delete(self, ids=None, **kwargs):
        deleted.append(list(ids or []))
        return real_delete(self, ids=ids, **kwargs)

    monkeypatch.setattr(FAISS, "delete", spy_delete)

    _make_processor(content_dir, output_dir).process_documents()

    assert deleted and set(deleted[0]) == set(b_ids)
    manifest_after = mf.load_manifest(output_dir)
    assert "b.html" not in manifest_after.files
    assert "a.html" in manifest_after.files


# --------------------------------------------------------------------------- #
# 6. Pipeline fingerprint changed — auto full rebuild + warning
# --------------------------------------------------------------------------- #


def test_pipeline_fingerprint_change_triggers_full_rebuild(corpus, patch_docling, caplog) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _make_processor(content_dir, output_dir).process_documents()

    # Second run with a DIFFERENT max_tokens → fingerprint differs.
    processor = dl.DocumentProcessor(
        embeddings=_DeterministicEmbed(),
        content_dir=str(content_dir),
        output_dir=str(output_dir),
        max_tokens=1024,  # was 512 → different fingerprint
        num_workers=1,
        incremental=True,
        embedder_model="test/embedder",
        embedder_dim=8,
    )
    with caplog.at_level("WARNING"):
        processor.process_documents()

    assert any("fingerprint changed" in record.getMessage().lower() for record in caplog.records)
    # Manifest now carries the new fingerprint.
    manifest = mf.load_manifest(output_dir)
    assert "max_tokens=1024" in manifest.pipeline_fingerprint


# --------------------------------------------------------------------------- #
# 7. Manifest corrupted — fallback to full rebuild
# --------------------------------------------------------------------------- #


def test_corrupt_manifest_falls_back_to_full_rebuild(corpus, patch_docling, caplog) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _make_processor(content_dir, output_dir).process_documents()

    # Corrupt the manifest.
    (output_dir / mf.MANIFEST_FILENAME).write_text("{ not json ", encoding="utf-8")

    with caplog.at_level("WARNING"):
        _make_processor(content_dir, output_dir).process_documents()

    # Manifest got rebuilt cleanly.
    manifest = mf.load_manifest(output_dir)
    assert manifest is not None
    assert "a.html" in manifest.files


# --------------------------------------------------------------------------- #
# 8. --full flag (incremental=False) forces rebuild even with a valid manifest
# --------------------------------------------------------------------------- #


def test_incremental_false_ignores_manifest(corpus, patch_docling, monkeypatch) -> None:
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _make_processor(content_dir, output_dir).process_documents()

    # Add a file that the incremental run would normally pick up.
    _write(content_dir / "b.html")

    docling_calls: list[list[str]] = []
    original = dl.DocumentProcessor._run_docling_on

    def spy(self, files):
        docling_calls.append([Path(f).name for f in files])
        return original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_run_docling_on", spy)

    _make_processor(content_dir, output_dir, incremental=False).process_documents()

    # Full rebuild means ALL files go through Docling (not just b.html).
    assert sorted(docling_calls[-1]) == ["a.html", "b.html"]
