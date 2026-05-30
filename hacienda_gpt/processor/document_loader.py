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

Two ingest modes coexist:

* **Full rebuild** (no manifest, or pipeline fingerprint changed): every file
  in ``content_dir`` is parsed and embedded; the manifest is written from
  scratch.
* **Incremental** (manifest matches current pipeline): each file is SHA-256'd
  and compared to the manifest. Only new + modified files run Docling + the
  embedder; FAISS receives an ``add_documents`` for new chunks and a
  ``delete`` for chunks whose source disappeared or changed. The 12-hour
  cold-start ingest collapses to ~minutes for typical daily updates.

Heavy imports (``docling`` / ``langchain_docling`` / FAISS) are deferred so
that importing this module never drags in the ML stack — unit tests stub the
loader seam instead.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing as mp
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from hacienda_gpt.processor.manifest import (
    FileDiff,
    FileEntry,
    Manifest,
    compute_file_hash,
    compute_pipeline_fingerprint,
    diff_files_against_manifest,
    load_manifest,
    relative_key,
    save_manifest,
    update_manifest_after_run,
)

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


def _shard_files(files: list[str], num_shards: int) -> list[list[str]]:
    """Round-robin split (kept for back-compat / tests). Not used by the
    work-stealing pool, which pulls one file at a time from a shared queue."""
    return [files[i::num_shards] for i in range(num_shards)]


# Per-worker process-local state. Populated by `_init_worker` once per process
# (multiprocessing.Pool initializer) and reused for every file that worker
# pulls off the queue. Stashing it here avoids rebuilding the HybridChunker
# (which loads the embedder's tokenizer) for every single document.
_WORKER_CHUNKER: Any = None


def _init_worker(max_tokens: int | None) -> None:
    """Pool initializer — runs ONCE per worker process.

    Builds the HybridChunker (loads the embedder tokenizer ~50-200 MB) and
    wires up `configure_logging` so progress messages from this worker actually
    surface in the parent's piped stdout. Without `configure_logging`, the
    `Finished converting document X` lines from Docling — the only signal the
    user has to gauge progress — get dropped by Python's default WARNING-level
    root logger inside spawned children.
    """
    global _WORKER_CHUNKER

    from docling.chunking import HybridChunker

    from hacienda_gpt import settings as _settings
    from hacienda_gpt.utils import configure_logging

    configure_logging()

    chunker_kwargs: dict[str, Any] = {"tokenizer": _settings.EMBEDDING_MODEL}
    if max_tokens:
        chunker_kwargs["max_tokens"] = max_tokens
    _WORKER_CHUNKER = HybridChunker(**chunker_kwargs)
    logging.info("Docling worker ready (pid=%d, max_tokens=%s)", _pid(), max_tokens)


def _pid() -> int:
    import os

    return os.getpid()


def _stable_chunk_id(file_path: str, chunk_index: int) -> str:
    """Generate a deterministic FAISS chunk id keyed on the source file.

    Format: ``<sha256(file_path)[:12]>-<chunk_index>``. 12 hex chars give
    ~2.8 × 10¹⁴ buckets — collision-free in practice for our 11K-file
    corpus, and stable across reruns of the same file path.

    Why this matters: when a file is re-ingested (modified), we must be
    able to ask FAISS to delete the previous chunks for that file. The id
    is stored in the manifest's ``chunk_ids`` field; reusing the same
    formula on a new run produces the same ids only as a sanity property,
    but the source of truth is always the manifest.
    """
    file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:12]
    return f"{file_hash}-{chunk_index}"


def _process_one_file(file_path: str) -> tuple[str, list[Document]]:
    """Worker entry point — pulled once per file from the pool's input iterable.

    Reuses the per-process `_WORKER_CHUNKER` so each call only pays for
    `DoclingLoader([file], ...).load()` (a thin LangChain wrapper around
    `DocumentConverter.convert(file)`), not for the tokenizer load.

    Stamps each emitted ``Document`` with:

    * ``chunk_id``: stable FAISS id (see :func:`_stable_chunk_id`).
    * ``source_file``: the absolute path that produced this chunk — the
      manifest layer uses it to group chunks per file when computing diffs.

    Returns `(file_path, chunks)` so the parent can log per-file progress
    in the order documents actually finish (with `imap_unordered`).
    """
    from langchain_docling.loader import DoclingLoader, ExportType

    if _WORKER_CHUNKER is None:  # Defensive: should never happen with initializer.
        _init_worker(None)
    loader = DoclingLoader(
        file_path=[file_path],
        export_type=ExportType.DOC_CHUNKS,
        chunker=_WORKER_CHUNKER,
    )
    raw_chunks = loader.load()
    docs: list[Document] = []
    for idx, chunk in enumerate(raw_chunks):
        meta = docling_chunk_metadata(chunk)
        meta["chunk_id"] = _stable_chunk_id(file_path, idx)
        meta["source_file"] = file_path
        docs.append(Document(page_content=chunk.page_content, metadata=meta))
    return (file_path, docs)


class DocumentProcessor:
    """Build a FAISS index from a content directory using Docling."""

    def __init__(
        self,
        embeddings: Embeddings,
        content_dir: str,
        output_dir: str,
        *,
        max_tokens: int | None = None,
        num_workers: int = 1,
        incremental: bool = True,
        embedder_model: str | None = None,
        embedder_dim: int | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.content_dir = content_dir
        self.output_dir = output_dir
        self.max_tokens = max_tokens
        self.num_workers = max(1, num_workers)
        self.incremental = incremental
        # Pipeline-identifying knobs used to detect when an existing manifest
        # was built against a different model/chunker and must be discarded.
        self._embedder_model = embedder_model or "unknown"
        self._embedder_dim = embedder_dim

    def pipeline_fingerprint(self) -> str:
        """Stable identifier for the (embedder, chunker, docling) tuple.

        Comparing this against the manifest's stored fingerprint tells us
        whether the existing FAISS vectors can be reused as-is or whether a
        full rebuild is required.
        """
        # ``docling`` doesn't expose ``__version__`` on its public module
        # surface, so fall back to package metadata. Without this the
        # fingerprint stays ``docling=unknown`` across upgrades and a Docling
        # bump silently reuses incompatible vectors.
        try:
            from importlib.metadata import PackageNotFoundError, version

            docling_version = version("docling")
        except (PackageNotFoundError, Exception):  # noqa: BLE001
            docling_version = "unknown"
        return compute_pipeline_fingerprint(
            embedder_model=self._embedder_model,
            embedder_dim=self._embedder_dim,
            max_tokens=self.max_tokens,
            docling_version=docling_version,
        )

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

    def _load_chunks_sequential(self, files: list[str]) -> list[Document]:
        """Single-process variant.

        Builds the DoclingLoader for the whole batch at once. The result
        doesn't carry per-chunk source info, so we group chunks by their
        Docling-reported origin filename to assign stable ids. That keeps
        the chunk_id contract identical to the parallel path.
        """
        raw_chunks = self._build_loader(files).load()
        chunks_per_file: dict[str, int] = {}
        docs: list[Document] = []
        # langchain-docling tags each Document with ``dl_meta.origin.filename``
        # — see docling_chunk_metadata. We resolve that back to the absolute
        # input path via the discovered file list (name → path).
        files_by_basename = {Path(f).name: f for f in files}
        for chunk in raw_chunks:
            meta = docling_chunk_metadata(chunk)
            dl_meta = _as_dl_meta(chunk.metadata.get("dl_meta") if isinstance(chunk.metadata, dict) else None)
            origin_name = _origin_filename(dl_meta) or ""
            source = files_by_basename.get(origin_name, origin_name)
            idx = chunks_per_file.get(source, 0)
            chunks_per_file[source] = idx + 1
            meta["chunk_id"] = _stable_chunk_id(source, idx)
            meta["source_file"] = source
            docs.append(Document(page_content=chunk.page_content, metadata=meta))
        return docs

    def _load_chunks_parallel(self, files: list[str]) -> list[Document]:
        """Fan files out across `num_workers` processes with work-stealing.

        The pool's initializer loads HybridChunker once per worker process;
        each `_process_one_file` call then pulls the next pending file from
        the shared queue (`imap_unordered(..., chunksize=1)`), processes it,
        and emits `(file, chunks)` to the parent. Workers never sit idle
        waiting for a peer's huge BOE consolidado to finish, which is the
        failure mode the round-robin static split used to hit.

        Docling's own ThreadPoolExecutor variant is flagged "no benefit
        expected without free-threaded python" — the GIL serialises Docling's
        HTML pipeline. multiprocessing sidesteps the GIL by giving each
        worker its own interpreter, which is what we want.

        The embedder model is **never** loaded in workers — only in the
        parent at FAISS-build time. Only the tokenizer-driven chunker lives
        per-worker.
        """
        total = len(files)
        n_workers = min(self.num_workers, total)
        logging.info(
            "Spawning %d Docling workers (work-stealing pool over %d files)",
            n_workers,
            total,
        )
        ctx = mp.get_context("spawn")  # macOS-safe: avoids fork() + threads issues.
        all_chunks: list[Document] = []
        processed = 0
        last_progress_logged = 0
        # Report every ~2% of files, at most every 100 files, at least every 25.
        report_every = max(25, min(100, total // 50 or 25))
        with ctx.Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(self.max_tokens,),
        ) as pool:
            for file_path, file_chunks in pool.imap_unordered(_process_one_file, files, chunksize=1):
                processed += 1
                all_chunks.extend(file_chunks)
                if processed - last_progress_logged >= report_every or processed == total:
                    pct = 100 * processed / total
                    logging.info(
                        "Docling progress: %d / %d files (%.1f%%) — %d chunks so far",
                        processed,
                        total,
                        pct,
                        len(all_chunks),
                    )
                    last_progress_logged = processed
        return all_chunks

    def load_chunks(self) -> list[Document]:
        """Run Docling on every file in ``content_dir`` (no incremental diff)."""
        files = self.discover_files()
        return self._run_docling_on(files)

    def _run_docling_on(self, files: list[str]) -> list[Document]:
        if not files:
            return []
        logging.info(
            "Converting & chunking %d files with Docling (num_workers=%d)",
            len(files),
            self.num_workers,
        )
        if self.num_workers <= 1 or len(files) <= 1:
            return self._load_chunks_sequential(files)
        return self._load_chunks_parallel(files)

    # ----------------------------------------------------------------- #
    # Manifest helpers
    # ----------------------------------------------------------------- #

    def _build_entries(
        self, files_processed: list[str], chunks: list[Document]
    ) -> dict[str, FileEntry]:
        """Group new chunks by their source file, then mint manifest entries.

        ``files_processed`` is the authoritative list of files we asked
        Docling to ingest (the diff's ``new + modified``). Every entry must
        appear in the manifest even if Docling produced zero chunks for it,
        so a subsequent run still recognises the file as known.
        """
        content_dir = Path(self.content_dir)
        chunks_by_file: dict[str, list[str]] = {}
        for chunk in chunks:
            src = chunk.metadata.get("source_file")
            cid = chunk.metadata.get("chunk_id")
            if src and cid:
                chunks_by_file.setdefault(src, []).append(cid)

        now = datetime.now(UTC).isoformat()
        entries: dict[str, FileEntry] = {}
        for path in files_processed:
            try:
                sha, size = compute_file_hash(Path(path))
            except FileNotFoundError:
                # Race: file removed mid-run. Skip — the manifest just won't
                # list it, and the next run sees it correctly.
                logging.warning("Skipping manifest entry for missing file %s", path)
                continue
            rel = relative_key(path, content_dir)
            cids = chunks_by_file.get(path, [])
            entries[rel] = FileEntry(
                sha256=sha,
                size_bytes=size,
                n_chunks=len(cids),
                chunk_ids=cids,
                ingested_at=now,
            )
        return entries

    # ----------------------------------------------------------------- #
    # Diff-aware pipeline
    # ----------------------------------------------------------------- #

    def plan(
        self,
    ) -> tuple[list[Document], list[str], FileDiff | None, dict[str, FileEntry]]:
        """Compute what needs to happen to bring FAISS in sync with disk.

        Returns
        -------
        new_chunks : list[Document]
            Documents to add to the index (from new or modified source files).
        ids_to_remove : list[str]
            FAISS chunk ids to drop because their source file was removed
            or modified.
        diff : FileDiff | None
            ``None`` signals "fall back to full rebuild" (no manifest, or the
            stored fingerprint doesn't match the current pipeline). Otherwise
            carries the new/modified/removed/unchanged split.
        new_entries : dict[str, FileEntry]
            Manifest entries to insert or update for the files we just
            processed. Keys are relative paths from ``content_dir``.
        """
        files = self.discover_files()
        fingerprint = self.pipeline_fingerprint()
        manifest: Manifest | None = None

        if self.incremental:
            manifest = load_manifest(self.output_dir)
            if manifest is not None and manifest.pipeline_fingerprint != fingerprint:
                logging.warning(
                    "Pipeline fingerprint changed (was=%r, now=%r). Forcing full reindex.",
                    manifest.pipeline_fingerprint,
                    fingerprint,
                )
                manifest = None

        if manifest is None:
            # Full rebuild path.
            new_chunks = self._run_docling_on(files)
            new_entries = self._build_entries(files, new_chunks)
            return new_chunks, [], None, new_entries

        diff = diff_files_against_manifest(files, manifest, Path(self.content_dir))
        logging.info(
            "Incremental diff vs manifest: new=%d  modified=%d  removed=%d  unchanged=%d",
            len(diff.new),
            len(diff.modified),
            len(diff.removed),
            len(diff.unchanged),
        )

        # Collect FAISS ids to drop for files that vanished or changed.
        ids_to_remove: list[str] = []
        content_dir = Path(self.content_dir)
        for path in list(diff.modified) + list(diff.removed):
            rel = relative_key(path, content_dir)
            entry = manifest.files.get(rel)
            if entry:
                ids_to_remove.extend(entry.chunk_ids)

        files_to_process = list(diff.new) + list(diff.modified)
        new_chunks = self._run_docling_on(files_to_process) if files_to_process else []
        new_entries = self._build_entries(files_to_process, new_chunks)
        return new_chunks, ids_to_remove, diff, new_entries

    def process_documents(self) -> None:
        from langchain_community.vectorstores import FAISS

        logging.info("Loading documents from %s", self.content_dir)
        new_chunks, ids_to_remove, diff, new_entries = self.plan()

        fingerprint = self.pipeline_fingerprint()
        content_dir = Path(self.content_dir)

        if diff is None:
            # Full rebuild — build a fresh index with explicit ids so future
            # incremental runs can target individual chunks.
            logging.info("Produced %d Docling chunks for indexing (full rebuild)", len(new_chunks))
            if not new_chunks:
                raise RuntimeError(
                    f"No indexable chunks produced from {self.content_dir!r}; nothing to index."
                )
            ids = [c.metadata["chunk_id"] for c in new_chunks]
            db = FAISS.from_documents(new_chunks, self.embeddings, ids=ids)
            self._atomic_save_index(db)
            manifest = update_manifest_after_run(
                manifest=None,
                pipeline_fingerprint=fingerprint,
                content_dir=content_dir,
                diff=None,
                new_entries=new_entries,
            )
            save_manifest(self.output_dir, manifest)
            logging.info("Local FAISS index successfully saved to %s", self.output_dir)
            return

        # Incremental path.
        if not diff.has_work:
            logging.info("Corpus unchanged since last run — nothing to do.")
            existing = load_manifest(self.output_dir)
            if existing is not None:
                save_manifest(self.output_dir, existing)  # bump updated_at
            return

        try:
            db = FAISS.load_local(
                self.output_dir, self.embeddings, allow_dangerous_deserialization=True
            )
        except Exception as exc:  # noqa: BLE001
            logging.error(
                "Could not load existing FAISS index at %s (%s); falling back to full rebuild.",
                self.output_dir,
                exc,
            )
            all_files = self.discover_files()
            all_chunks = self._run_docling_on(all_files)
            ids = [c.metadata["chunk_id"] for c in all_chunks]
            db = FAISS.from_documents(all_chunks, self.embeddings, ids=ids)
            self._atomic_save_index(db)
            manifest = update_manifest_after_run(
                manifest=None,
                pipeline_fingerprint=fingerprint,
                content_dir=content_dir,
                diff=None,
                new_entries=self._build_entries(all_files, all_chunks),
            )
            save_manifest(self.output_dir, manifest)
            logging.info("Local FAISS index rebuilt at %s", self.output_dir)
            return

        if ids_to_remove:
            db.delete(ids=ids_to_remove)
            logging.info("Deleted %d stale chunks from FAISS.", len(ids_to_remove))
        if new_chunks:
            new_ids = [c.metadata["chunk_id"] for c in new_chunks]
            db.add_documents(new_chunks, ids=new_ids)
            logging.info("Appended %d new chunks to FAISS.", len(new_chunks))

        self._atomic_save_index(db)
        existing = load_manifest(self.output_dir)
        manifest = update_manifest_after_run(
            manifest=existing,
            pipeline_fingerprint=fingerprint,
            content_dir=content_dir,
            diff=diff,
            new_entries=new_entries,
        )
        save_manifest(self.output_dir, manifest)
        logging.info("Local FAISS index updated incrementally at %s", self.output_dir)

    # ----------------------------------------------------------------- #
    # I/O
    # ----------------------------------------------------------------- #

    def _atomic_save_index(self, db: Any) -> None:
        """Save FAISS files via a temp dir + rename so a crash mid-write
        never leaves a half-written ``index.faiss`` or ``index.pkl`` next
        to a stale manifest."""
        target = Path(self.output_dir)
        target.mkdir(parents=True, exist_ok=True)
        tmp_dir = target.parent / (target.name + ".faiss.tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            db.save_local(str(tmp_dir))
            for name in ("index.faiss", "index.pkl"):
                src = tmp_dir / name
                if src.exists():
                    dst = target / name
                    if dst.exists():
                        dst.unlink()
                    src.replace(dst)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)


def build_index(args: dict) -> None:
    """Build and persist the FAISS index using the configured local embedder."""
    # Lazy import keeps the processor module (and its CLI) light, and routes
    # through the shared embedder factory so the index always matches the
    # query path.
    from hacienda_gpt import settings
    from hacienda_gpt.llm.embeddings import create_embeddings

    # Capture the embedder identity at construction time so the processor can
    # compute the pipeline fingerprint without re-importing settings later.
    args.setdefault("embedder_model", settings.EMBEDDING_MODEL)
    raw_dim = settings.EMBEDDING_DIM
    args.setdefault("embedder_dim", int(raw_dim) if raw_dim else None)

    processor = DocumentProcessor(create_embeddings(), **args)
    processor.process_documents()
