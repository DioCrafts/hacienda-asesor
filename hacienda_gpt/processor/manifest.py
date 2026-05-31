"""Manifest layer that tracks which files are already in the FAISS index.

When ingestion runs incrementally we need to answer two questions per file:

1. **Is the bytes-on-disk version equal to what we last indexed?** That's a
   SHA-256 comparison against the manifest entry. Match → skip Docling +
   embedding entirely.
2. **If a file changed or was removed, which FAISS vector IDs should be
   dropped?** That's what the per-file ``chunk_ids`` field stores.

The manifest is a plain JSON file (``manifest.json`` inside the FAISS output
directory) so it survives crashes, is git-diffable, and doesn't need any
extra runtime dependency. Save is **atomic**: write to ``manifest.json.tmp``
and rename on success.

A separate ``pipeline_fingerprint`` string captures every knob that would
invalidate existing vectors if it changed (embedder model, MRL truncation,
chunker max_tokens, Docling major version). If that fingerprint differs
between the run and the stored manifest, callers must treat the manifest as
absent and rebuild from scratch — silently reusing vectors across pipeline
changes would mix incompatible vector spaces and break retrieval without
any warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


MANIFEST_FILENAME = "manifest.json"
SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 1024 * 1024  # 1 MiB stream


@dataclass(frozen=True)
class FileEntry:
    """Per-file record kept inside the manifest.

    ``chunk_ids`` is what lets the diff step delete just this file's vectors
    from FAISS without rebuilding the entire index when the file is later
    modified or removed.

    ``strategy_id`` records which ingest strategy produced these chunks.
    When the strategy responsible for a file (per the current registry)
    differs from the strategy stored here, the diff treats the file as
    **modified** and re-ingests it — letting strategy changes invalidate
    only the files they own without forcing a full rebuild. Legacy
    manifests (pre-strategy) carry ``None`` here, which is treated as
    "trust the existing entry, don't reprocess on strategy mismatch
    alone" — backward-compatible migration without losing indexed work.
    """

    sha256: str
    size_bytes: int
    n_chunks: int
    chunk_ids: list[str]
    ingested_at: str  # ISO-8601 UTC
    strategy_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "n_chunks": self.n_chunks,
            "chunk_ids": list(self.chunk_ids),
            "ingested_at": self.ingested_at,
        }
        # Persist strategy_id only when set, keeping legacy entries clean
        # JSON (so a manifest from before the strategies feature reads as
        # ``strategy_id=None`` after a roundtrip).
        if self.strategy_id is not None:
            out["strategy_id"] = self.strategy_id
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FileEntry:
        raw_strategy = raw.get("strategy_id")
        return cls(
            sha256=str(raw["sha256"]),
            size_bytes=int(raw["size_bytes"]),
            n_chunks=int(raw["n_chunks"]),
            chunk_ids=[str(x) for x in raw.get("chunk_ids", [])],
            ingested_at=str(raw["ingested_at"]),
            strategy_id=str(raw_strategy) if raw_strategy is not None else None,
        )


@dataclass
class Manifest:
    pipeline_fingerprint: str
    created_at: str
    updated_at: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def total_chunks(self) -> int:
        return sum(entry.n_chunks for entry in self.files.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stats": {
                "n_files": len(self.files),
                "n_chunks": self.total_chunks,
            },
            "files": {path: entry.to_dict() for path, entry in self.files.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        if int(raw.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported manifest schema_version={raw.get('schema_version')!r}; "
                f"expected {SCHEMA_VERSION}. Delete the manifest to force a full reindex."
            )
        return cls(
            pipeline_fingerprint=str(raw["pipeline_fingerprint"]),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
            files={path: FileEntry.from_dict(entry) for path, entry in (raw.get("files") or {}).items()},
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class FileDiff:
    """Result of comparing the current corpus against a stored manifest.

    Paths inside the lists are **absolute filesystem paths** to make the
    caller's job trivial (it can `open()` them directly), but inside the
    manifest we store paths relative to ``content_dir`` for portability.
    """

    new: list[str]
    modified: list[str]
    removed: list[str]
    unchanged: list[str]

    @property
    def has_work(self) -> bool:
        return bool(self.new or self.modified or self.removed)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def compute_file_hash(path: Path) -> tuple[str, int]:
    """Stream the file through SHA-256 and report size in one pass.

    Streaming keeps memory bounded even for the multi-MB BOE consolidados:
    we never hold the full file in RAM, just one 1 MiB buffer at a time.
    """
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(_HASH_CHUNK_BYTES)
            if not block:
                break
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


def compute_pipeline_fingerprint(
    *,
    embedder_model: str,
    embedder_dim: int | None,
    max_tokens: int | None,
    docling_version: str,
    loader_schema: int = 1,
) -> str:
    """Deterministic string capturing every knob that invalidates vectors.

    Order is fixed so we can compare strings directly. Booleans like
    "auto" appear when the corresponding field is unset, so empty/None
    values still produce a stable, distinct fingerprint.
    """
    parts = [
        f"embedder={embedder_model}",
        f"dim={embedder_dim or 'auto'}",
        f"max_tokens={max_tokens or 'auto'}",
        f"docling={docling_version}",
        f"loader_schema={loader_schema}",
    ]
    return "|".join(parts)


def manifest_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / MANIFEST_FILENAME


def load_manifest(output_dir: str | Path) -> Manifest | None:
    """Read the manifest if present and valid. Returns None otherwise.

    A corrupt or unreadable manifest is treated as "no manifest" — the
    caller then falls back to a full rebuild. We log loud so users see why
    a costly reindex was triggered.
    """
    path = manifest_path(output_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Manifest.from_dict(raw)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(
            "Could not load manifest at %s (%s); will treat as missing and rebuild from scratch.",
            path,
            exc,
        )
        return None


def save_manifest(output_dir: str | Path, manifest: Manifest) -> Path:
    """Atomically persist the manifest next to the FAISS files.

    Atomic = write to ``manifest.json.tmp`` and ``os.replace`` over the
    target. Crash mid-write leaves the previous manifest intact; never
    half-written JSON.
    """
    out_path = manifest_path(output_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    manifest.updated_at = _now_iso()
    tmp_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, out_path)
    logger.info("Saved manifest with %d files / %d chunks → %s", len(manifest.files), manifest.total_chunks, out_path)
    return out_path


def _to_relative(path: str, content_dir: Path) -> str:
    """Manifest stores paths *relative to content_dir* for portability.

    Moving ``data/html/`` to a different disk should not break the manifest.
    Anything outside content_dir (rare) is stored as the absolute path
    so the caller still has a stable lookup key.
    """
    try:
        return str(Path(path).resolve().relative_to(content_dir.resolve()))
    except ValueError:
        return str(Path(path).resolve())


def diff_files_against_manifest(
    current_files: list[str],
    manifest: Manifest | None,
    content_dir: Path,
    *,
    strategy_for: callable | None = None,  # type: ignore[type-arg]
) -> FileDiff:
    """Three-way diff between disk state and manifest state.

    Cost: O(N) for new/missing files, plus one SHA-256 stream per file
    that matches a manifest entry by path. We short-circuit on size
    mismatch before hashing so size-only changes don't pay the hashing
    cost.

    When ``strategy_for`` is supplied (a callable ``(abs_path) -> str``
    that returns the strategy_id that currently claims the file), files
    whose stored ``strategy_id`` differs from the current claim are
    marked as **modified** — the strategy change invalidates that file's
    chunks without forcing a full rebuild of the rest of the index.
    Manifest entries with ``strategy_id is None`` (legacy, pre-strategy
    feature) are left alone — we trust the existing chunks rather than
    forcing a costly migration on every legacy index.
    """
    if manifest is None:
        # First-time ingest — everything is new.
        return FileDiff(new=list(current_files), modified=[], removed=[], unchanged=[])

    current_by_rel = {_to_relative(p, content_dir): p for p in current_files}
    manifest_keys = set(manifest.files.keys())

    new: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    for rel, abs_path in current_by_rel.items():
        path_obj = Path(abs_path)
        try:
            stat = path_obj.stat()
        except FileNotFoundError:
            # Race: caller listed a file that vanished before we got to it.
            # Silently skip — the next run picks up reality.
            continue
        if rel not in manifest_keys:
            new.append(abs_path)
            continue
        prev = manifest.files[rel]
        if stat.st_size != prev.size_bytes:
            modified.append(abs_path)
            continue
        sha, _ = compute_file_hash(path_obj)
        if sha != prev.sha256:
            modified.append(abs_path)
            continue
        # SHA matches. Re-check strategy ownership: if the file is now
        # claimed by a different strategy than the one stored, the file
        # must be reprocessed by the new owner. Legacy entries
        # (strategy_id=None) skip this gate to avoid forcing a full
        # migration on existing indexes.
        if strategy_for is not None and prev.strategy_id is not None:
            try:
                current_sid = strategy_for(abs_path)
            except Exception:  # noqa: BLE001
                current_sid = prev.strategy_id  # fail-open on routing errors
            if current_sid != prev.strategy_id:
                modified.append(abs_path)
                continue
        unchanged.append(abs_path)

    removed_rels = manifest_keys - set(current_by_rel.keys())
    removed = [str(content_dir / rel) for rel in sorted(removed_rels)]
    return FileDiff(new=sorted(new), modified=sorted(modified), removed=removed, unchanged=sorted(unchanged))


def update_manifest_after_run(
    manifest: Manifest | None,
    *,
    pipeline_fingerprint: str,
    content_dir: Path,
    diff: FileDiff | None,
    new_entries: dict[str, FileEntry],
) -> Manifest:
    """Compose the manifest to persist after a successful run.

    On a full rebuild (``diff is None``), the manifest is replaced
    wholesale from ``new_entries``. On an incremental run, the manifest is
    preserved and only the affected entries (modified / removed) are
    touched. ``new_entries`` keys are **relative paths from content_dir**
    so the caller controls the key normalisation.
    """
    if manifest is None or diff is None:
        return Manifest(
            pipeline_fingerprint=pipeline_fingerprint,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            files=dict(new_entries),
        )

    files = dict(manifest.files)
    for path in diff.removed:
        rel = _to_relative(path, content_dir)
        files.pop(rel, None)
    files.update(new_entries)
    manifest.pipeline_fingerprint = pipeline_fingerprint
    manifest.files = files
    manifest.updated_at = _now_iso()
    return manifest


def relative_key(path: str | Path, content_dir: Path) -> str:
    """Public re-export so the loader and tests agree on the key format."""
    return _to_relative(str(path), content_dir)
