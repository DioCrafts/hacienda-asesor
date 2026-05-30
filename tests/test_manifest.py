"""Manifest layer — unit coverage for the incremental indexing contract.

These tests pin the bits that, if they regress, would either let the
indexer skip files it shouldn't (silent data loss) or reprocess files it
shouldn't (wasted hours of Docling work). They run without touching FAISS,
Docling, or any embedder.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hacienda_gpt.processor import manifest as mf


# --------------------------------------------------------------------------- #
# Hashing + fingerprint
# --------------------------------------------------------------------------- #


def test_compute_file_hash_is_deterministic_and_reports_size(tmp_path: Path) -> None:
    file = tmp_path / "x.html"
    file.write_bytes(b"hello world")
    h1, size1 = mf.compute_file_hash(file)
    h2, size2 = mf.compute_file_hash(file)
    assert h1 == h2
    assert size1 == size2 == len(b"hello world")
    # SHA-256 of "hello world" — pin the expected value so we catch
    # accidental algorithm changes (e.g. someone swapping to MD5).
    assert h1 == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_compute_file_hash_changes_with_one_byte(tmp_path: Path) -> None:
    f = tmp_path / "x.html"
    f.write_bytes(b"hello")
    h_a, _ = mf.compute_file_hash(f)
    f.write_bytes(b"hellp")  # one byte changed
    h_b, _ = mf.compute_file_hash(f)
    assert h_a != h_b


def test_compute_pipeline_fingerprint_changes_per_knob() -> None:
    base = mf.compute_pipeline_fingerprint(
        embedder_model="Qwen/Qwen3-Embedding-0.6B",
        embedder_dim=None,
        max_tokens=512,
        docling_version="2.96.0",
    )
    # Same args → same fingerprint.
    assert base == mf.compute_pipeline_fingerprint(
        embedder_model="Qwen/Qwen3-Embedding-0.6B",
        embedder_dim=None,
        max_tokens=512,
        docling_version="2.96.0",
    )
    # Each knob changes the fingerprint.
    other_model = mf.compute_pipeline_fingerprint(
        embedder_model="Qwen/Qwen3-Embedding-8B",
        embedder_dim=None,
        max_tokens=512,
        docling_version="2.96.0",
    )
    assert other_model != base
    other_max = mf.compute_pipeline_fingerprint(
        embedder_model="Qwen/Qwen3-Embedding-0.6B",
        embedder_dim=None,
        max_tokens=1024,
        docling_version="2.96.0",
    )
    assert other_max != base
    other_dim = mf.compute_pipeline_fingerprint(
        embedder_model="Qwen/Qwen3-Embedding-0.6B",
        embedder_dim=1024,
        max_tokens=512,
        docling_version="2.96.0",
    )
    assert other_dim != base


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _sample_manifest() -> mf.Manifest:
    now = datetime.now(UTC).isoformat()
    return mf.Manifest(
        pipeline_fingerprint="test-fp",
        created_at=now,
        updated_at=now,
        files={
            "boe-bulk/2024/BOE-A-2024-1.html": mf.FileEntry(
                sha256="aa" * 32,
                size_bytes=1024,
                n_chunks=3,
                chunk_ids=["aa-0", "aa-1", "aa-2"],
                ingested_at=now,
            ),
        },
    )


def test_load_manifest_returns_none_when_absent(tmp_path: Path) -> None:
    assert mf.load_manifest(tmp_path) is None


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    original = _sample_manifest()
    saved_path = mf.save_manifest(tmp_path, original)
    assert saved_path.exists()
    loaded = mf.load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.pipeline_fingerprint == original.pipeline_fingerprint
    assert loaded.files.keys() == original.files.keys()
    entry = loaded.files["boe-bulk/2024/BOE-A-2024-1.html"]
    assert entry.sha256 == "aa" * 32
    assert entry.chunk_ids == ["aa-0", "aa-1", "aa-2"]


def test_load_manifest_logs_and_returns_none_on_corrupt_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Corrupt manifest → caller falls back to full rebuild + we shout
    loudly so a costly rebuild isn't silent."""
    (tmp_path / mf.MANIFEST_FILENAME).write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert mf.load_manifest(tmp_path) is None
    assert any("Could not load manifest" in record.message for record in caplog.records)


def test_load_manifest_rejects_unknown_schema_version(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Future-schema manifest must NOT be silently downgraded; we want a
    full rebuild rather than load-with-missing-fields."""
    payload = _sample_manifest().to_dict()
    payload["schema_version"] = 99
    (tmp_path / mf.MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert mf.load_manifest(tmp_path) is None
    assert any("Unsupported manifest schema_version" in record.message or "Could not load manifest" in record.message for record in caplog.records)


def test_save_manifest_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash mid-save must not corrupt the existing manifest.

    We force `os.replace` to raise after the tmp file is written and verify
    the existing manifest is untouched.
    """
    # First, a clean save so there's an existing manifest to protect.
    original = _sample_manifest()
    mf.save_manifest(tmp_path, original)
    original_bytes = (tmp_path / mf.MANIFEST_FILENAME).read_bytes()

    # Now simulate a crash inside os.replace during the next save.
    real_replace = mf.os.replace

    def bomb(src, dst):
        raise OSError("disk full simulation")

    monkeypatch.setattr(mf.os, "replace", bomb)
    updated = _sample_manifest()
    updated.pipeline_fingerprint = "different-fp"
    with pytest.raises(OSError):
        mf.save_manifest(tmp_path, updated)
    monkeypatch.setattr(mf.os, "replace", real_replace)

    # Existing manifest is exactly what we wrote first.
    assert (tmp_path / mf.MANIFEST_FILENAME).read_bytes() == original_bytes


# --------------------------------------------------------------------------- #
# Diff against manifest
# --------------------------------------------------------------------------- #


def _populate_corpus(tmp_path: Path, name_to_content: dict[str, bytes]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for rel_name, content in name_to_content.items():
        path = tmp_path / rel_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths[rel_name] = path
    return paths


def test_diff_with_no_manifest_marks_everything_new(tmp_path: Path) -> None:
    paths = _populate_corpus(tmp_path, {"a.html": b"A", "b.html": b"B"})
    diff = mf.diff_files_against_manifest(
        current_files=[str(p) for p in paths.values()],
        manifest=None,
        content_dir=tmp_path,
    )
    assert sorted(diff.new) == sorted(str(p) for p in paths.values())
    assert diff.modified == []
    assert diff.removed == []
    assert diff.unchanged == []
    assert diff.has_work is True


def test_diff_detects_unchanged_files(tmp_path: Path) -> None:
    content = b"hello world"
    file = tmp_path / "x.html"
    file.write_bytes(content)
    sha, size = mf.compute_file_hash(file)
    manifest = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="now",
        updated_at="now",
        files={
            "x.html": mf.FileEntry(
                sha256=sha, size_bytes=size, n_chunks=1, chunk_ids=["x-0"], ingested_at="now",
            ),
        },
    )
    diff = mf.diff_files_against_manifest(
        current_files=[str(file)], manifest=manifest, content_dir=tmp_path,
    )
    assert diff.unchanged == [str(file)]
    assert diff.new == []
    assert diff.modified == []
    assert diff.removed == []
    assert diff.has_work is False


def test_diff_detects_modified_file_by_content_when_size_matches(tmp_path: Path) -> None:
    """Edge case: same size, different content. SHA must catch it (size
    short-circuit is just an optimisation, not a substitute)."""
    f = tmp_path / "x.html"
    f.write_bytes(b"hello")  # size 5
    sha, size = mf.compute_file_hash(f)
    manifest = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="now",
        updated_at="now",
        files={
            "x.html": mf.FileEntry(
                sha256=sha, size_bytes=size, n_chunks=1, chunk_ids=["x-0"], ingested_at="now",
            ),
        },
    )
    # Same size, different bytes.
    f.write_bytes(b"hellp")
    diff = mf.diff_files_against_manifest(
        current_files=[str(f)], manifest=manifest, content_dir=tmp_path,
    )
    assert diff.modified == [str(f)]
    assert diff.unchanged == []


def test_diff_detects_modified_by_size_without_hashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When sizes differ, we never bother to SHA — important for the 800KB
    BOE consolidados where streaming hash is non-trivial."""
    f = tmp_path / "x.html"
    f.write_bytes(b"hello")
    manifest = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="now",
        updated_at="now",
        files={
            "x.html": mf.FileEntry(
                sha256="z" * 64, size_bytes=999_999, n_chunks=1, chunk_ids=["x-0"], ingested_at="now",
            ),
        },
    )
    hash_calls: list[Path] = []
    real_hash = mf.compute_file_hash

    def spy_hash(path: Path):
        hash_calls.append(path)
        return real_hash(path)

    monkeypatch.setattr(mf, "compute_file_hash", spy_hash)
    diff = mf.diff_files_against_manifest(
        current_files=[str(f)], manifest=manifest, content_dir=tmp_path,
    )
    assert diff.modified == [str(f)]
    assert hash_calls == [], "size mismatch must short-circuit the hash"


def test_diff_detects_new_and_removed_files(tmp_path: Path) -> None:
    paths = _populate_corpus(tmp_path, {"a.html": b"A", "b.html": b"B"})
    sha_a, size_a = mf.compute_file_hash(paths["a.html"])
    # Manifest knew about a.html and c.html (which no longer exists).
    manifest = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="now",
        updated_at="now",
        files={
            "a.html": mf.FileEntry(
                sha256=sha_a, size_bytes=size_a, n_chunks=1, chunk_ids=["a-0"], ingested_at="now",
            ),
            "c.html": mf.FileEntry(
                sha256="ff" * 32, size_bytes=10, n_chunks=2, chunk_ids=["c-0", "c-1"], ingested_at="now",
            ),
        },
    )
    diff = mf.diff_files_against_manifest(
        current_files=[str(p) for p in paths.values()], manifest=manifest, content_dir=tmp_path,
    )
    assert diff.unchanged == [str(paths["a.html"])]
    assert diff.new == [str(paths["b.html"])]
    assert diff.modified == []
    # removed comes back as an absolute path under content_dir.
    assert diff.removed == [str(tmp_path / "c.html")]


def test_diff_handles_file_vanishing_between_discover_and_diff(tmp_path: Path) -> None:
    """If a file is discovered then deleted by another process before we
    stat it, we must not crash — we silently skip and let the next run
    pick it up."""
    f = tmp_path / "x.html"
    f.write_bytes(b"hi")
    manifest = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="now",
        updated_at="now",
        files={},
    )
    current = [str(f), str(tmp_path / "ghost.html")]  # ghost.html doesn't exist
    diff = mf.diff_files_against_manifest(
        current_files=current, manifest=manifest, content_dir=tmp_path,
    )
    # The ghost file was new on disk but vanished — should not appear anywhere.
    assert str(tmp_path / "ghost.html") not in diff.new + diff.modified + diff.removed + diff.unchanged
    # f.html should be marked new.
    assert diff.new == [str(f)]


# --------------------------------------------------------------------------- #
# update_manifest_after_run
# --------------------------------------------------------------------------- #


def test_update_manifest_full_rebuild_replaces_everything(tmp_path: Path) -> None:
    """When diff is None (full rebuild path), we wipe the manifest and
    rewrite from `new_entries`."""
    new_entries = {
        "z.html": mf.FileEntry(
            sha256="z" * 64, size_bytes=10, n_chunks=1, chunk_ids=["z-0"], ingested_at="now",
        ),
    }
    result = mf.update_manifest_after_run(
        manifest=None,
        pipeline_fingerprint="fp-new",
        content_dir=tmp_path,
        diff=None,
        new_entries=new_entries,
    )
    assert result.pipeline_fingerprint == "fp-new"
    assert result.files == new_entries


def test_update_manifest_incremental_preserves_unchanged_drops_removed_adds_new(tmp_path: Path) -> None:
    base = mf.Manifest(
        pipeline_fingerprint="fp",
        created_at="t0",
        updated_at="t0",
        files={
            "stable.html": mf.FileEntry(
                sha256="s" * 64, size_bytes=10, n_chunks=1, chunk_ids=["s-0"], ingested_at="t0",
            ),
            "gone.html": mf.FileEntry(
                sha256="g" * 64, size_bytes=10, n_chunks=2, chunk_ids=["g-0", "g-1"], ingested_at="t0",
            ),
        },
    )
    # Diff: gone.html was removed, new.html was added.
    diff = mf.FileDiff(
        new=[str(tmp_path / "new.html")],
        modified=[],
        removed=[str(tmp_path / "gone.html")],
        unchanged=[str(tmp_path / "stable.html")],
    )
    new_entries = {
        "new.html": mf.FileEntry(
            sha256="n" * 64, size_bytes=20, n_chunks=3, chunk_ids=["n-0", "n-1", "n-2"], ingested_at="t1",
        ),
    }
    result = mf.update_manifest_after_run(
        manifest=base,
        pipeline_fingerprint="fp",
        content_dir=tmp_path,
        diff=diff,
        new_entries=new_entries,
    )
    assert set(result.files.keys()) == {"stable.html", "new.html"}
    # stable.html survives untouched.
    assert result.files["stable.html"].chunk_ids == ["s-0"]
    # new.html has the freshly-ingested entry.
    assert result.files["new.html"].n_chunks == 3


def test_relative_key_normalises_to_content_dir(tmp_path: Path) -> None:
    abs_file = tmp_path / "sub" / "x.html"
    abs_file.parent.mkdir(parents=True)
    abs_file.write_bytes(b"")
    assert mf.relative_key(str(abs_file), tmp_path) == "sub/x.html"
