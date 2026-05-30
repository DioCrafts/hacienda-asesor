"""Parallelism contract for the Docling-backed `DocumentProcessor`.

The single-core CPU bottleneck we measured on a 10-core M-series box (Python
holding the GIL through Docling's HTML pipeline) only goes away when each
worker runs in its own interpreter. These tests pin the seams:

- Round-robin sharding gives every worker a balanced workload.
- `num_workers=1` keeps the original sequential code path (regression guard
  for the existing single-core ingest behaviour).
- The CLI surfaces `--num-workers` and threads it down into the processor.

We don't actually spawn worker processes here — that would require shipping
a real Docling-installed runtime to CI. The pool entry point is exercised by
calling `_process_shard_worker` directly with a stubbed loader.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from langchain_core.documents import Document

from hacienda_gpt.cli import processor as cli_module
from hacienda_gpt.processor import document_loader as loader_module
from hacienda_gpt.processor.document_loader import DocumentProcessor, _shard_files


class _NoOpEmbeddings:
    def embed_documents(self, texts):
        return [[0.0] for _ in texts]

    def embed_query(self, text):
        return [0.0]


def _processor(num_workers: int = 1, max_tokens: int | None = 512) -> DocumentProcessor:
    return DocumentProcessor(
        embeddings=_NoOpEmbeddings(),
        content_dir=".",
        output_dir=".",
        max_tokens=max_tokens,
        num_workers=num_workers,
    )


# --------------------------------------------------------------------------- #
# Sharding
# --------------------------------------------------------------------------- #


def test_shard_files_round_robin_is_balanced() -> None:
    files = [f"f{i}.html" for i in range(11)]
    shards = _shard_files(files, 4)
    assert len(shards) == 4
    sizes = sorted(len(s) for s in shards)
    # 11 files / 4 workers ⇒ sizes are {2,3,3,3}: max-min ≤ 1.
    assert max(sizes) - min(sizes) <= 1
    # Every file appears exactly once.
    flat = [f for s in shards for f in s]
    assert sorted(flat) == sorted(files)


def test_shard_files_round_robin_handles_more_workers_than_files() -> None:
    files = ["only.html"]
    shards = _shard_files(files, 4)
    # 1 worker gets the file; the other 3 are empty — handled in load_chunks.
    non_empty = [s for s in shards if s]
    assert non_empty == [["only.html"]]


# --------------------------------------------------------------------------- #
# Worker entry point (no actual multiprocessing — call directly)
# --------------------------------------------------------------------------- #


def test_process_one_file_uses_per_worker_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_process_one_file` must reuse the pre-built `_WORKER_CHUNKER` (set by
    `_init_worker`) instead of building one per call. Otherwise the tokenizer
    is loaded on every file — that's the original sequential bottleneck
    reappearing inside each pool task.
    """
    from hacienda_gpt.processor import document_loader as dl

    sentinel_chunker = object()
    monkeypatch.setattr(dl, "_WORKER_CHUNKER", sentinel_chunker)

    captured: dict = {}

    class _FakeLoader:
        def __init__(self, *, file_path, export_type, chunker):
            captured["file_path"] = file_path
            captured["chunker"] = chunker

        def load(self):
            return []

    class _FakeLoaderModule:
        DoclingLoader = _FakeLoader

        class ExportType:
            DOC_CHUNKS = "doc_chunks"

    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_docling.loader",
        _FakeLoaderModule,
    )
    fp, chunks = dl._process_one_file("/tmp/some.html")
    assert fp == "/tmp/some.html"
    assert chunks == []
    assert captured["chunker"] is sentinel_chunker, "must reuse the per-process chunker"
    assert captured["file_path"] == ["/tmp/some.html"]


def test_init_worker_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """`configure_logging` MUST run inside the worker; otherwise the
    `Finished converting document X` lines stay invisible — that's exactly
    the visibility regression we just fixed."""
    from hacienda_gpt.processor import document_loader as dl

    calls = {"configure_logging": 0, "chunker_built": 0}

    class _FakeChunker:
        def __init__(self, **kw):
            calls["chunker_built"] += 1
            calls["chunker_kwargs"] = kw

    fake_settings = type("S", (), {"EMBEDDING_MODEL": "stub/embedder"})()

    def fake_configure_logging() -> None:
        calls["configure_logging"] += 1

    monkeypatch.setitem(
        __import__("sys").modules,
        "docling.chunking",
        type("M", (), {"HybridChunker": _FakeChunker}),
    )
    monkeypatch.setattr(
        __import__("hacienda_gpt.utils", fromlist=["configure_logging"]),
        "configure_logging",
        fake_configure_logging,
    )
    monkeypatch.setattr(dl, "_WORKER_CHUNKER", None)
    # The function imports `from hacienda_gpt import settings`; replace at the
    # package level so the worker picks our stub.
    monkeypatch.setattr("hacienda_gpt.settings.EMBEDDING_MODEL", "stub/embedder", raising=False)

    dl._init_worker(512)
    assert calls["configure_logging"] == 1
    assert calls["chunker_built"] == 1
    assert calls["chunker_kwargs"]["max_tokens"] == 512
    assert dl._WORKER_CHUNKER is not None


# --------------------------------------------------------------------------- #
# load_chunks routing
# --------------------------------------------------------------------------- #


def test_load_chunks_num_workers_1_uses_sequential_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-parallelism behaviour must be preserved when num_workers=1."""
    proc = _processor(num_workers=1)
    monkeypatch.setattr(proc, "discover_files", lambda: ["/tmp/a.html", "/tmp/b.html"])

    called: dict[str, int] = {"seq": 0, "par": 0}
    monkeypatch.setattr(
        proc,
        "_load_chunks_sequential",
        lambda files: called.__setitem__("seq", called["seq"] + 1) or [Document(page_content="x", metadata={})],
    )
    monkeypatch.setattr(
        proc,
        "_load_chunks_parallel",
        lambda files: called.__setitem__("par", called["par"] + 1) or [],
    )

    proc.load_chunks()
    assert called == {"seq": 1, "par": 0}


def test_load_chunks_num_workers_gt1_uses_parallel_path(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _processor(num_workers=4)
    monkeypatch.setattr(proc, "discover_files", lambda: [f"/tmp/{i}.html" for i in range(8)])

    called: dict[str, int] = {"seq": 0, "par": 0}
    monkeypatch.setattr(
        proc,
        "_load_chunks_sequential",
        lambda files: called.__setitem__("seq", called["seq"] + 1) or [],
    )
    monkeypatch.setattr(
        proc,
        "_load_chunks_parallel",
        lambda files: called.__setitem__("par", called["par"] + 1) or [Document(page_content="x", metadata={})],
    )

    proc.load_chunks()
    assert called == {"seq": 0, "par": 1}


def test_load_chunks_single_file_skips_pool_even_with_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool overhead isn't worth it for a single file — sequential wins
    regardless of `num_workers`."""
    proc = _processor(num_workers=8)
    monkeypatch.setattr(proc, "discover_files", lambda: ["/tmp/only.html"])

    called: dict[str, int] = {"seq": 0, "par": 0}
    monkeypatch.setattr(
        proc,
        "_load_chunks_sequential",
        lambda files: called.__setitem__("seq", called["seq"] + 1) or [Document(page_content="x", metadata={})],
    )
    monkeypatch.setattr(
        proc,
        "_load_chunks_parallel",
        lambda files: called.__setitem__("par", called["par"] + 1) or [],
    )

    proc.load_chunks()
    assert called == {"seq": 1, "par": 0}


def test_num_workers_floor_is_one() -> None:
    proc = _processor(num_workers=0)
    assert proc.num_workers == 1


# --------------------------------------------------------------------------- #
# CLI option
# --------------------------------------------------------------------------- #


def test_cli_threads_num_workers_into_build_index(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    captured: dict = {}

    def fake_build_index(args):
        captured.update(args)

    monkeypatch.setattr(cli_module, "build_index", fake_build_index)
    content_dir = tmp_path / "html"
    content_dir.mkdir()
    output_dir = tmp_path / "faiss"

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "--content-dir",
            str(content_dir),
            "--output-dir",
            str(output_dir),
            "--max-tokens",
            "512",
            "--num-workers",
            "8",
            "--overwrite-output",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("num_workers") == 8
    assert captured.get("max_tokens") == 512


def test_cli_num_workers_default_targets_mac_perf_cores(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Default `--num-workers` is 11 — tuned for Mac M-series (10 perf cores +
    1 extra to absorb stalls on giant BOE consolidados). Bumping the default
    matters because every CLI invocation that omits the flag was running
    single-process before; now it runs on the whole performance core pool
    automatically. If this default ever drops, ingest wall time grows ~10x."""
    captured: dict = {}
    monkeypatch.setattr(cli_module, "build_index", lambda args: captured.update(args))
    content_dir = tmp_path / "html"
    content_dir.mkdir()
    output_dir = tmp_path / "faiss"
    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "--content-dir",
            str(content_dir),
            "--output-dir",
            str(output_dir),
            "--overwrite-output",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("num_workers") == 11
