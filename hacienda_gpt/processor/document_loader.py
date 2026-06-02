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
import queue
import shutil
import threading
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
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    from hacienda_gpt import settings as _settings
    from hacienda_gpt.utils import configure_logging

    configure_logging()

    # Docling ≥ 2.50 validates the HybridChunker(tokenizer=str) path by
    # trying to fetch ``sentence_bert_config.json`` from HF Hub for our
    # local MLX model. That fetch fails (path is local, no Hub entry), and
    # the chunker raises "max_tokens could not be determined automatically".
    # We bypass the auto-detect by constructing the HuggingFaceTokenizer
    # ourselves with an explicit ``max_tokens`` budget. The default matches
    # the manifest's pipeline_fingerprint (max_tokens=512) so an unset
    # ``--max-tokens`` CLI flag keeps the existing index valid.
    effective_max_tokens = max_tokens or 512
    tokenizer = HuggingFaceTokenizer.from_pretrained(
        model_name=_settings.EMBEDDING_MODEL,
        max_tokens=effective_max_tokens,
    )
    _WORKER_CHUNKER = HybridChunker(tokenizer=tokenizer)
    logging.info("Docling worker ready (pid=%d, max_tokens=%s)", _pid(), effective_max_tokens)


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


def _docling_parse_paths(file_paths: list[str]) -> list[Document]:
    """Run the per-worker Docling+HybridChunker over a list of paths.

    Shared helper between the default ingest strategy (one parent file
    in, one batch of chunks out) and the BOE consolidado strategy
    (parent split into N temp sub-files; this helper chunks all N in one
    DoclingLoader call, which is faster than N separate calls because
    Docling reuses its converter state).

    Reuses ``_WORKER_CHUNKER`` from the pool initialiser — does not
    reload the tokenizer per call. Chunks are returned with Docling's
    raw metadata (``dl_meta`` + the flattened ``title``/``section``/
    ``source_url`` fields); callers are responsible for stamping
    ``chunk_id`` and ``source_file`` to match their semantics.
    """
    from langchain_docling.loader import DoclingLoader, ExportType

    if _WORKER_CHUNKER is None:
        # The pool initialiser must have set this. Failing loudly here
        # surfaces a configuration bug; silently rebuilding would
        # silently drop the chunker tuning (max_tokens, etc.).
        raise RuntimeError(
            "_WORKER_CHUNKER is not initialised — pool initialiser did not run."
        )
    loader = DoclingLoader(
        file_path=list(file_paths),
        export_type=ExportType.DOC_CHUNKS,
        chunker=_WORKER_CHUNKER,
    )
    raw_chunks = loader.load()
    out: list[Document] = []
    for chunk in raw_chunks:
        meta = docling_chunk_metadata(chunk)
        out.append(Document(page_content=chunk.page_content, metadata=meta))
    return out


def _docling_parse_default(file_path: str) -> list[Document]:
    """Default Docling strategy's concrete parse step.

    Surfaced here (rather than inside the strategy class) so that
    workers can call it via the per-process ``_WORKER_CHUNKER`` without
    the strategy needing to know about the pool internals. Returns
    chunks with ``source_file`` + ``chunk_id`` stamped.
    """
    chunks = _docling_parse_paths([file_path])
    docs: list[Document] = []
    for idx, chunk in enumerate(chunks):
        meta = dict(chunk.metadata or {})
        meta["chunk_id"] = _stable_chunk_id(file_path, idx)
        meta["source_file"] = file_path
        meta.setdefault("_strategy_id", "default-docling-v1")
        docs.append(Document(page_content=chunk.page_content, metadata=meta))
    return docs


def _process_one_file(file_path: str) -> tuple[str, list[Document]]:
    """Worker entry point — pulled once per file from the pool's input
    iterable. Dispatches the file to the appropriate ingest strategy
    via the registry (specialised strategies first, default last).

    Returns ``(file_path, chunks)`` so the parent can log per-file
    progress in the order documents actually finish (with
    ``imap_unordered``).
    """
    from hacienda_gpt.processor.strategies import (
        install_default_strategies,
        select_strategy,
    )

    # Idempotent — only registers strategies the worker hasn't seen yet.
    # Each spawned worker re-imports the module so we ensure the registry
    # is populated even when the worker's process started without it.
    install_default_strategies()
    strategy = select_strategy(file_path)
    docs = list(strategy.parse(file_path))
    # Defence-in-depth: stamp ``chunk_id`` / ``source_file`` if the
    # strategy forgot to. The contract says strategies must set them,
    # but a fallback keeps a buggy strategy from breaking the index.
    for idx, doc in enumerate(docs):
        doc.metadata.setdefault("chunk_id", _stable_chunk_id(file_path, idx))
        doc.metadata.setdefault("source_file", file_path)
        doc.metadata.setdefault("_strategy_id", strategy.strategy_id)
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
        batch_size: int = 200,
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
        # Files persisted to FAISS + manifest after every batch. Smaller =
        # finer crash-recovery granularity; larger = fewer (cheaper) save_local
        # calls. 200 is a balance for the 11k-file corpus: ~95s of work at
        # risk if we crash mid-batch on M-series hardware.
        self.batch_size = max(1, batch_size)

    def pipeline_fingerprint(self) -> str:
        """Stable identifier for the (embedder, chunker, docling) tuple.

        Comparing this against the manifest's stored fingerprint tells us
        whether the existing FAISS vectors can be reused as-is or whether a
        full rebuild is required.
        """
        # ``docling`` doesn't expose ``__version__`` on its public module
        # surface, so fall back to package metadata. Without this the
        # fingerprint stays ``docling=unknown`` across upgrades and a Docling
        # bump silently reuses incompatible vectors. We catch **only**
        # ``PackageNotFoundError`` — anything else (corrupt package metadata,
        # importlib internals failing) must bubble up and fail loudly: a
        # silent ``unknown`` would defeat the whole point of the fingerprint
        # protection against pipeline-version mismatches.
        from importlib.metadata import PackageNotFoundError, version

        try:
            docling_version = version("docling")
        except PackageNotFoundError:
            docling_version = "unknown"

        # Contextual embeddings are an index-time transformation of the
        # text that gets embedded — turning them on/off (or swapping the
        # generator model) means the FAISS vectors no longer match what a
        # query at runtime would produce. Include in the fingerprint so
        # the manifest layer forces a clean rebuild.
        from hacienda_gpt import settings

        contextual_model = None
        contextual_max_tokens = None
        if settings.CONTEXTUAL_EMBEDDINGS_ENABLED:
            contextual_model = settings.CONTEXTUAL_MODEL_PATH
            contextual_max_tokens = settings.CONTEXTUAL_MAX_TOKENS

        return compute_pipeline_fingerprint(
            embedder_model=self._embedder_model,
            embedder_dim=self._embedder_dim,
            max_tokens=self.max_tokens,
            docling_version=docling_version,
            contextual_model=contextual_model,
            contextual_max_tokens=contextual_max_tokens,
        )

    def discover_files(self) -> list[str]:
        root = Path(self.content_dir)
        if not root.exists():
            return []
        return [str(path) for path in sorted(root.rglob("*")) if path.suffix.lower() in SUPPORTED_SUFFIXES]

    def _build_loader(self, files: list[str]) -> Any:
        """Construct the DoclingLoader. Isolated so tests can stub the seam."""
        from docling.chunking import HybridChunker
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        from langchain_docling.loader import DoclingLoader, ExportType

        from hacienda_gpt import settings

        # Same explicit-tokenizer construction as ``_init_worker``: Docling
        # ≥ 2.50 can't auto-detect ``max_tokens`` for the local MLX model
        # because it tries an HF Hub fetch that fails offline.
        tokenizer = HuggingFaceTokenizer.from_pretrained(
            model_name=settings.EMBEDDING_MODEL,
            max_tokens=self.max_tokens or 512,
        )
        return DoclingLoader(
            file_path=files,
            export_type=ExportType.DOC_CHUNKS,
            chunker=HybridChunker(tokenizer=tokenizer),
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
        """Collect *all* chunks for ``files`` synchronously.

        Kept as a thin wrapper around the streaming primitive for callers
        that still want the whole-batch shape (smoke tests, the legacy
        ``load_chunks`` path). New ingest orchestration goes through
        :meth:`_iter_chunks_streaming` directly so workers never idle between
        batches.
        """
        if not files:
            return []
        logging.info(
            "Converting & chunking %d files with Docling (num_workers=%d)",
            len(files),
            self.num_workers,
        )
        all_chunks: list[Document] = []
        for _, chunks in self._iter_chunks_streaming(files):
            all_chunks.extend(chunks)
        return all_chunks

    def _iter_chunks_streaming(self, files: list[str]):
        """Yield ``(file, chunks)`` as Docling workers finish each file.

        Single persistent multiprocessing pool over the full ``files`` list:
        workers always pull from the **global** queue, so the "fat tail"
        idle problem (three workers stuck on giant BOE consolidados while
        eight peers wait for the next batch) goes away — the eight free
        peers just take the next file from the global queue immediately.

        Used by :meth:`process_documents` to interleave Docling parsing
        and FAISS / manifest checkpointing without any batch-boundary
        synchronisation barrier. The HybridChunker is loaded once per
        worker for the **entire** iteration (vs once per batch in the
        legacy batched implementation).
        """
        if not files:
            return

        if self.num_workers <= 1 or len(files) <= 1:
            # Sequential / single-file path. Route through
            # ``_process_one_file`` per file (in-process, no
            # multiprocessing) so the **strategy registry** dispatches
            # the same way it does for the parallel path — otherwise a
            # giant BOE on a num_workers=1 ingest would bypass the
            # BoE-consolidado splitter and hang.
            _init_worker(self.max_tokens)  # ensure _WORKER_CHUNKER is set
            for f in files:
                try:
                    file_path, chunks = _process_one_file(f)
                except Exception as exc:  # noqa: BLE001
                    logging.error(
                        "Sequential strategy dispatch failed on %s: %s",
                        f,
                        exc,
                    )
                    continue
                yield file_path, chunks
            return

            # Legacy (unreachable): pre-strategy path kept commented for
            # reference. Remove once the strategy registry has been in
            # production for a release without regressions.
            all_chunks = self._load_chunks_sequential(files)  # pragma: no cover
            chunks_by_file: dict[str, list[Document]] = {}
            for chunk in all_chunks:
                src = chunk.metadata.get("source_file", "")
                chunks_by_file.setdefault(src, []).append(chunk)
            for f in files:
                yield f, chunks_by_file.get(f, [])
            return

        total = len(files)
        n_workers = min(self.num_workers, total)
        logging.info(
            "Spawning %d Docling workers (whole-corpus streaming pool over %d files)",
            n_workers,
            total,
        )
        processed = 0
        last_progress_logged = 0
        cum_chunks = 0
        # Report every ~2 % of files, at most every 100 files, at least every 25.
        report_every = max(25, min(100, total // 50 or 25))
        ctx = mp.get_context("spawn")  # macOS-safe: avoids fork+threads issues.
        with ctx.Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(self.max_tokens,),
        ) as pool:
            for file_path, file_chunks in pool.imap_unordered(_process_one_file, files, chunksize=1):
                processed += 1
                cum_chunks += len(file_chunks)
                if processed - last_progress_logged >= report_every or processed == total:
                    pct = 100 * processed / total
                    logging.info(
                        "Docling streaming progress: %d / %d files (%.1f%%) — %d chunks so far",
                        processed,
                        total,
                        pct,
                        cum_chunks,
                    )
                    last_progress_logged = processed
                yield file_path, file_chunks

    # ----------------------------------------------------------------- #
    # Manifest helpers
    # ----------------------------------------------------------------- #

    def _build_entries(
        self, files_processed: list[str], chunks: list[Document]
    ) -> dict[str, FileEntry]:
        """Group new chunks by their source file, then mint manifest entries.

        ``files_processed`` is the authoritative list of files we asked the
        chosen strategy to ingest (the diff's ``new + modified``). Every
        entry must appear in the manifest even if the strategy produced
        zero chunks for it, so a subsequent run still recognises the file
        as known.

        Records the ``strategy_id`` that emitted each chunk by reading
        ``chunk.metadata["_strategy_id"]`` (stamped by ``_process_one_file``
        and the strategies themselves). When a file processed produced no
        chunks at all, we fall back to the registry's current claim for
        that path so the manifest still records who would have processed
        it — keeps the per-file strategy diff coherent on edge cases.
        """
        content_dir = Path(self.content_dir)
        chunks_by_file: dict[str, list[str]] = {}
        strategy_by_file: dict[str, str] = {}
        for chunk in chunks:
            src = chunk.metadata.get("source_file")
            cid = chunk.metadata.get("chunk_id")
            sid = chunk.metadata.get("_strategy_id")
            if src and cid:
                chunks_by_file.setdefault(src, []).append(cid)
            if src and sid and src not in strategy_by_file:
                strategy_by_file[src] = sid

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
            sid = strategy_by_file.get(path)
            if sid is None:
                # Zero-chunk file — ask the registry who claims this path
                # now so we still record the strategy_id correctly.
                try:
                    from hacienda_gpt.processor.strategies import (
                        install_default_strategies,
                        select_strategy,
                    )

                    install_default_strategies()
                    sid = select_strategy(path).strategy_id
                except Exception:  # noqa: BLE001 — never block the ingest on this
                    sid = None
            entries[rel] = FileEntry(
                sha256=sha,
                size_bytes=size,
                n_chunks=len(cids),
                chunk_ids=cids,
                ingested_at=now,
                strategy_id=sid,
            )
        return entries

    # ----------------------------------------------------------------- #
    # Diff-aware pipeline
    # ----------------------------------------------------------------- #

    def plan(self) -> tuple[list[str], list[str], FileDiff | None, str]:
        """Decide what work to do — read-only, never runs Docling.

        Returns
        -------
        files_to_process : list[str]
            Absolute paths to feed through Docling + the embedder.
        ids_to_remove : list[str]
            FAISS chunk ids to drop (sources removed or modified).
        diff : FileDiff | None
            ``None`` signals "no usable manifest, do a full rebuild".
            Otherwise carries the new/modified/removed/unchanged split.
        fingerprint : str
            Pipeline fingerprint to stamp on any manifest we write later.
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
            # No usable manifest → full rebuild over every discovered file.
            return list(files), [], None, fingerprint

        # Pass the strategy router into the diff so files whose owning
        # strategy changed get re-ingested by the new owner.
        def _strategy_for(path: str) -> str:
            from hacienda_gpt.processor.strategies import (
                install_default_strategies,
                select_strategy,
            )

            install_default_strategies()
            return select_strategy(path).strategy_id

        diff = diff_files_against_manifest(
            files,
            manifest,
            Path(self.content_dir),
            strategy_for=_strategy_for,
        )
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
        return files_to_process, ids_to_remove, diff, fingerprint

    def process_documents(self) -> None:
        """Bring FAISS + manifest in sync with disk, batching for crash safety.

        Persistence model: after every batch of ``self.batch_size`` files we
        ``save_local`` the FAISS index and rewrite ``manifest.json``. A crash
        between batches loses at most one batch of work; relaunching with
        ``--incremental`` (default) sees the manifest, SHA-256-matches the
        already-indexed files as ``unchanged``, and resumes at the first
        missing file. The save order is **index first, manifest second**: if
        we crash between them the index has extra chunks the manifest doesn't
        know about, and the next run re-indexes those files — FAISS overwrites
        the entries (deterministic chunk_ids). The opposite order would mark
        files indexed without vectors, which silently breaks retrieval.

        Concurrency: a single ingest run holds an advisory ``fcntl.flock``
        on ``<output_dir>/.processor.lock`` for its entire duration. A
        second invocation that finds the lock already held exits with a
        clear error instead of corrupting the FAISS+manifest pair — we
        learned this the hard way after three parallel runs overwrote
        each other's atomic saves and shrank a 2.6 GB index to 19 MB.
        """
        import fcntl
        import os

        from langchain_community.vectorstores import FAISS

        # Acquire the processor lock for the lifetime of this run.
        # ``flock`` is automatically released by the OS when the file
        # descriptor closes (normal exit, crash, kill), so we don't need
        # a finally-block — staleness self-resolves.
        os.makedirs(self.output_dir, exist_ok=True)
        lock_path = Path(self.output_dir) / ".processor.lock"
        lock_fd = open(lock_path, "w")  # noqa: SIM115 — released on process exit
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(
                f"Another processor instance already holds {lock_path}. "
                f"Concurrent ingests corrupt the FAISS+manifest pair. "
                f"Wait for the existing run to finish, or kill it before "
                f"retrying."
            ) from e

        logging.info("Loading documents from %s", self.content_dir)
        files_to_process, ids_to_remove, diff, fingerprint = self.plan()
        content_dir = Path(self.content_dir)

        # No-op: incremental run, nothing changed.
        if diff is not None and not diff.has_work:
            logging.info("Corpus unchanged since last run — nothing to do.")
            existing = load_manifest(self.output_dir)
            if existing is not None:
                save_manifest(self.output_dir, existing)
            return

        # Empty corpus on a full rebuild → loud failure.
        if diff is None and not files_to_process:
            raise RuntimeError(
                f"No indexable files found in {self.content_dir!r}; nothing to index."
            )

        # Set up starting state.
        # - Full rebuild (diff is None): db starts None and is lazy-initialized
        #   on the first batch with FAISS.from_documents.
        # - Incremental: load the existing index off disk. If load fails (e.g.
        #   corrupt files), fall back to full rebuild over the whole corpus.
        db: Any | None = None
        existing_manifest: Manifest | None = None
        if diff is not None:
            try:
                db = FAISS.load_local(
                    self.output_dir, self.embeddings, allow_dangerous_deserialization=True
                )
                existing_manifest = load_manifest(self.output_dir)
            except Exception as exc:  # noqa: BLE001
                logging.error(
                    "Could not load existing FAISS index at %s (%s); falling back to full rebuild.",
                    self.output_dir,
                    exc,
                )
                diff = None
                ids_to_remove = []
                db = None
                existing_manifest = None
                files_to_process = self.discover_files()
                if not files_to_process:
                    raise RuntimeError(
                        f"No indexable files found in {self.content_dir!r}; nothing to index."
                    ) from exc

        # Apply removals once, up front — they're a single bookkeeping step,
        # not per-batch work.
        if db is not None and ids_to_remove:
            db.delete(ids=ids_to_remove)
            logging.info("Deleted %d stale chunks from FAISS.", len(ids_to_remove))
        if existing_manifest is not None and diff is not None:
            for path in diff.removed:
                rel = relative_key(path, content_dir)
                existing_manifest.files.pop(rel, None)

        # Removals-only path: nothing to parse, just persist what we have.
        if not files_to_process:
            if db is not None:
                self._atomic_save_index(db)
            if existing_manifest is not None:
                existing_manifest.pipeline_fingerprint = fingerprint
                save_manifest(self.output_dir, existing_manifest)
            logging.info("Local FAISS index updated incrementally at %s", self.output_dir)
            return

        # Streaming processing loop with an **async checkpoint worker**.
        #
        # Sync checkpoints (the previous implementation) blocked the main
        # thread for the full embedding + FAISS-save duration, which on big
        # checkpoints (40 k chunks) took 30–60 minutes. During that window
        # the multiprocessing pool's IPC queue filled up and the 11 Docling
        # workers throttled from 99 % to ~70 % CPU because nothing was
        # consuming their output. The fix below moves the checkpoint work
        # (embedding + FAISS persist + manifest write) into a separate
        # thread; the main thread keeps draining the pool's queue, so the
        # workers never stall.
        #
        # The queue is bounded so memory growth is capped: at most
        # ``CHECKPOINT_QUEUE_SIZE`` checkpoints can be **waiting** to be
        # persisted while the worker processes one. Each waiting checkpoint
        # holds its window of chunks in memory (~200 files × ~150 chunks ×
        # 2 KB text ≈ 60 MB). Size 4 covers the ~6 : 1 parse-vs-embed time
        # ratio we measured on the BOE corpus on M-series — main thread can
        # fill 4 windows during one embedding cycle before blocking on
        # ``queue.put`` (natural backpressure).
        total_files = len(files_to_process)
        cumulative_chunks = 0
        checkpoint_n = 0
        files_window: list[str] = []
        chunks_window: list[Document] = []
        completed = 0

        # Mutable holders so the bg thread can update them as it goes — the
        # main thread only writes to ``db_holder`` before the loop starts
        # (during the initial removal pass) and reads it only in the
        # fallback rebuild branch above, so there is no race.
        db_holder: list[Any] = [db]
        manifest_holder: list[Manifest | None] = [existing_manifest]
        checkpoint_queue: queue.Queue = queue.Queue(maxsize=4)
        checkpoint_exc: list[BaseException] = []
        # SENTINEL signals "no more checkpoints, please exit".
        SENTINEL = object()

        def _persist_checkpoint(
            files_w: list[str],
            chunks_w: list[Document],
            ckpt_n: int,
        ) -> None:
            current_db = db_holder[0]
            current_manifest = manifest_holder[0]

            # Contextual prefix step: enrich each chunk's text BEFORE the
            # embedder sees it. This runs INSIDE the background checkpoint
            # thread (same one that drives FAISS.add_documents) so the
            # Docling workers are never blocked waiting on the contextual
            # LLM. The LLM and the embedder share the GPU sequentially,
            # but that's fine — each batch of 200 chunks is processed in
            # a tight loop, and the worker pool keeps streaming the next
            # batch in parallel.
            from hacienda_gpt import settings as _settings

            if (
                chunks_w
                and _settings.CONTEXTUAL_EMBEDDINGS_ENABLED
            ):
                from hacienda_gpt.processor.contextualizer import get_contextualizer

                contextualizer = get_contextualizer(
                    model_path=_settings.CONTEXTUAL_MODEL_PATH,
                    max_tokens=_settings.CONTEXTUAL_MAX_TOKENS,
                )
                t0 = datetime.now(UTC)
                chunks_w = list(contextualizer.contextualize(chunks_w))
                elapsed = (datetime.now(UTC) - t0).total_seconds()
                logging.info(
                    "Checkpoint %d: contextualized %d chunk(s) in %.1fs (~%.2fs/chunk).",
                    ckpt_n,
                    len(chunks_w),
                    elapsed,
                    elapsed / max(len(chunks_w), 1),
                )

            if chunks_w:
                ids = [c.metadata["chunk_id"] for c in chunks_w]
                if current_db is None:
                    current_db = FAISS.from_documents(
                        chunks_w, self.embeddings, ids=ids
                    )
                else:
                    current_db.add_documents(chunks_w, ids=ids)
                db_holder[0] = current_db

            if current_db is not None:
                self._atomic_save_index(current_db)

            entries = self._build_entries(files_w, chunks_w)
            if current_manifest is None:
                current_manifest = Manifest(
                    pipeline_fingerprint=fingerprint,
                    created_at=datetime.now(UTC).isoformat(),
                    updated_at=datetime.now(UTC).isoformat(),
                    files=dict(entries),
                )
            else:
                current_manifest.files.update(entries)
                current_manifest.pipeline_fingerprint = fingerprint
            manifest_holder[0] = current_manifest
            save_manifest(self.output_dir, current_manifest)

            logging.info(
                "Checkpoint %d persisted. Manifest: %d file(s).",
                ckpt_n,
                len(current_manifest.files),
            )

        def _checkpoint_worker() -> None:
            try:
                while True:
                    item = checkpoint_queue.get()
                    if item is SENTINEL:
                        return
                    files_w, chunks_w, ckpt_n = item
                    _persist_checkpoint(files_w, chunks_w, ckpt_n)
            except BaseException as exc:  # noqa: BLE001 - propagate to main
                checkpoint_exc.append(exc)

        logging.info(
            "Streaming over %d file(s) with %d worker(s); async-checkpoint every %d completed files.",
            total_files,
            self.num_workers,
            self.batch_size,
        )

        checkpoint_thread = threading.Thread(
            target=_checkpoint_worker,
            name="checkpoint-worker",
            daemon=True,
        )
        checkpoint_thread.start()

        try:
            for file_path, file_chunks in self._iter_chunks_streaming(files_to_process):
                # Surface bg-thread failures promptly so we don't accumulate
                # work that will never be persisted.
                if checkpoint_exc:
                    raise checkpoint_exc[0]

                files_window.append(file_path)
                chunks_window.extend(file_chunks)
                completed += 1
                cumulative_chunks += len(file_chunks)

                is_last = completed == total_files
                should_checkpoint = (completed % self.batch_size == 0) or is_last
                if not should_checkpoint:
                    continue

                checkpoint_n += 1
                logging.info(
                    "Checkpoint %d enqueued at file %d/%d: %d files, %d new chunks.",
                    checkpoint_n,
                    completed,
                    total_files,
                    len(files_window),
                    len(chunks_window),
                )
                # Blocks if the worker hasn't finished the previous
                # checkpoint yet — natural backpressure when embedding is
                # the bottleneck.
                checkpoint_queue.put((files_window, chunks_window, checkpoint_n))
                files_window = []
                chunks_window = []
        finally:
            # Signal end-of-stream and wait for the worker to drain whatever
            # is in flight. Even on exception we want the worker to finish
            # its current checkpoint so partial state on disk is consistent.
            #
            # Two failure modes to defend against:
            #
            # * Bg thread crashed (exception in ``_persist_checkpoint``) —
            #   it's no longer reading the queue, so ``put(SENTINEL)`` would
            #   block forever if the queue is full. Use a put with timeout
            #   inside a loop guarded by ``is_alive`` so we bail when the
            #   thread is gone.
            # * Bg thread is busy persisting a big checkpoint — perfectly
            #   normal, ``put`` will block until the worker drains a slot,
            #   then we join indefinitely. That is by design: we want all
            #   queued checkpoints persisted before returning.
            while checkpoint_thread.is_alive():
                try:
                    checkpoint_queue.put(SENTINEL, timeout=5)
                    break
                except queue.Full:
                    continue
            checkpoint_thread.join()

        # If the bg thread errored, re-raise here (it dies silently otherwise).
        if checkpoint_exc:
            raise checkpoint_exc[0]

        logging.info(
            "Local FAISS index successfully saved to %s (%d chunks across %d file(s))",
            self.output_dir,
            cumulative_chunks,
            total_files,
        )

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
    # The MLX backend always produces full-dimension vectors (no MRL
    # truncation). The fingerprint code records ``dim=auto`` when this is
    # None, which is correct for the current setup.
    args.setdefault("embedder_dim", None)

    processor = DocumentProcessor(create_embeddings(), **args)
    processor.process_documents()
