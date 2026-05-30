"""End-to-end coverage of the incremental indexing flow.

Stubs ``_iter_chunks_streaming`` (the primary parallelism seam) so we never
need the heavy Docling stack, but exercises the **real** FAISS code path
(langchain-community 0.4.1) so the add/delete semantics that the plan
depends on are validated against the actual library, not our wishful
thinking.

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


def _make_processor(
    content_dir: Path,
    output_dir: Path,
    *,
    incremental: bool = True,
    batch_size: int = 200,
) -> dl.DocumentProcessor:
    return dl.DocumentProcessor(
        embeddings=_DeterministicEmbed(),
        content_dir=str(content_dir),
        output_dir=str(output_dir),
        max_tokens=512,
        num_workers=1,
        incremental=incremental,
        embedder_model="test/embedder",
        embedder_dim=8,
        batch_size=batch_size,
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
    """Replace ``_iter_chunks_streaming`` with a predictable stub generator.

    The stub yields ``(file, chunks)`` with 2 chunks per .html file by
    default; tests can override the per-file chunk count via the returned
    ``override`` dict (set to 0 to simulate a file Docling failed to parse).
    """
    override: dict[str, int] = {}

    def fake_stream(self, files: list[str]):
        for f in files:
            count = override.get(Path(f).name, 2)
            yield f, _docs_for(f, count)

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", fake_stream)
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

    # Second run: instrument the streaming seam to fail if invoked at all.
    called: dict[str, list] = {"args": []}
    original = dl.DocumentProcessor._iter_chunks_streaming

    def spy(self, files):
        called["args"].append(list(files))
        yield from original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", spy)

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
    original = dl.DocumentProcessor._iter_chunks_streaming

    def spy(self, files):
        docling_calls.append([Path(f).name for f in files])
        yield from original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", spy)
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
    original = dl.DocumentProcessor._iter_chunks_streaming

    def spy(self, files):
        docling_calls.append([Path(f).name for f in files])
        yield from original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", spy)

    _make_processor(content_dir, output_dir, incremental=False).process_documents()

    # Full rebuild means ALL files go through Docling (not just b.html).
    assert sorted(docling_calls[-1]) == ["a.html", "b.html"]


# (The spy below replaces the earlier _run_docling_on-based pattern; kept for
# the test that follows because it uses the same fixture context.)


# --------------------------------------------------------------------------- #
# Batching: persistence is durable across batch boundaries.
# Crash mid-run loses at most one batch.
# --------------------------------------------------------------------------- #


def test_full_rebuild_persists_after_every_batch(corpus, patch_docling, monkeypatch) -> None:
    """5 files, batch_size=2 → 3 batches; manifest must grow monotonically
    after every batch (proving on-disk durability between batches)."""
    content_dir, output_dir = corpus
    for name in ("a.html", "b.html", "c.html", "d.html", "e.html"):
        _write(content_dir / name)

    manifest_snapshots: list[dict[str, int]] = []
    real_save_manifest = dl.save_manifest

    def snap(out_dir, manifest):
        manifest_snapshots.append({k: v.n_chunks for k, v in manifest.files.items()})
        return real_save_manifest(out_dir, manifest)

    monkeypatch.setattr(dl, "save_manifest", snap)

    _make_processor(content_dir, output_dir, batch_size=2).process_documents()

    # 5 files / batch_size 2 → batches of [2, 2, 1].
    # Manifest snapshot count = 3 (one save per batch). Each is a superset of
    # the previous (no batch ever shrinks the manifest during a clean run).
    assert len(manifest_snapshots) == 3
    sizes = [len(snap) for snap in manifest_snapshots]
    assert sizes == [2, 4, 5]
    for prev, curr in zip(manifest_snapshots, manifest_snapshots[1:]):
        assert set(prev.keys()).issubset(curr.keys())

    # Final manifest matches what's on disk.
    final = mf.load_manifest(output_dir)
    assert set(final.files.keys()) == {"a.html", "b.html", "c.html", "d.html", "e.html"}


def test_crash_mid_run_resumes_from_manifest(corpus, patch_docling, monkeypatch) -> None:
    """Simulate a crash mid-stream after the 4th file is yielded, then
    re-run incrementally.

    Verifies the streaming + checkpoint-counter semantics:
    - With ``batch_size=2`` the loop checkpoints after files 2 and 4
      (``completed % batch_size == 0``).
    - The streaming iterator raises before yielding file 5 → manifest has
      4 files (the two successful checkpoints).
    - The restart re-processes only the missing 1 file.
    """
    content_dir, output_dir = corpus
    for name in ("a.html", "b.html", "c.html", "d.html", "e.html"):
        _write(content_dir / name)

    original = dl.DocumentProcessor._iter_chunks_streaming

    def fail_after_four(self, files):
        yielded = 0
        for pair in original(self, files):
            if yielded == 4:
                raise RuntimeError("simulated crash mid-stream")
            yielded += 1
            yield pair

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", fail_after_four)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _make_processor(content_dir, output_dir, batch_size=2).process_documents()

    # After the crash, the manifest should reflect the 4 files from the two
    # successful checkpoints (at completed=2 and completed=4).
    mid = mf.load_manifest(output_dir)
    assert mid is not None
    assert len(mid.files) == 4

    # Re-run with the original (un-crashing) stub. Only the 1 missing file
    # should hit the streaming path on the restart.
    docling_calls: list[list[str]] = []

    def spy(self, files):
        docling_calls.append([Path(f).name for f in files])
        yield from original(self, files)

    monkeypatch.setattr(dl.DocumentProcessor, "_iter_chunks_streaming", spy)

    _make_processor(content_dir, output_dir, batch_size=2).process_documents()

    # On restart, only the one missing file is processed.
    flattened = [f for batch in docling_calls for f in batch]
    assert len(flattened) == 1
    final = mf.load_manifest(output_dir)
    assert len(final.files) == 5


def test_batch_with_zero_chunks_still_advances_manifest(corpus, patch_docling) -> None:
    """If Docling extracts zero chunks for some files in a batch (e.g. empty
    HTML pages mixed with normal ones), those files must still land in the
    manifest with ``n_chunks=0`` so the next run sees them as ``unchanged``
    and doesn't re-parse them forever."""
    content_dir, output_dir = corpus
    _write(content_dir / "a.html")
    _write(content_dir / "empty.html")

    # Override per-file chunk count: "empty.html" yields zero chunks.
    patch_docling["empty.html"] = 0

    _make_processor(content_dir, output_dir, batch_size=10).process_documents()

    manifest = mf.load_manifest(output_dir)
    assert set(manifest.files.keys()) == {"a.html", "empty.html"}
    assert manifest.files["a.html"].n_chunks == 2
    assert manifest.files["empty.html"].n_chunks == 0
    assert manifest.files["empty.html"].chunk_ids == []


def test_empty_corpus_full_rebuild_raises(corpus, patch_docling) -> None:
    """Pointing the processor at an empty content_dir is a loud failure —
    we never want to write an empty index silently."""
    content_dir, output_dir = corpus
    # content_dir exists but is empty.
    with pytest.raises(RuntimeError, match="No indexable files"):
        _make_processor(content_dir, output_dir).process_documents()


def test_batch_size_one_still_works(corpus, patch_docling) -> None:
    """Edge case: smallest possible batch size."""
    content_dir, output_dir = corpus
    for name in ("a.html", "b.html", "c.html"):
        _write(content_dir / name)

    _make_processor(content_dir, output_dir, batch_size=1).process_documents()

    final = mf.load_manifest(output_dir)
    assert set(final.files.keys()) == {"a.html", "b.html", "c.html"}
    # All chunks present.
    assert final.total_chunks == 6  # 3 files × 2 chunks/file (stub default)
