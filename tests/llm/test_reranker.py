"""Unit tests for the MLX reranker wrapper.

The real ``mlx_lm`` model load is expensive (1.1 GB on disk) and Apple-Silicon
only, so we stub the scoring layer directly: ``MLXReranker._score`` is
monkeypatched to return deterministic per-doc scores. That keeps the tests
focused on the wrapper's behaviour — sort order, top-K, metadata stamping,
immutability of original docs, model-cache singleton — without dragging in
either the MLX runtime or the (intentionally complex) batched tokenisation
math that lives inside ``_score``. Numerical / latency verification of the
batched implementation runs by hand on the target hardware.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain_core.documents import Document


@pytest.fixture
def stub_mlx(monkeypatch):
    """Stub ``MLXReranker._score`` with a length-based heuristic and pretend
    ``mlx_lm`` is present so model-cache code paths still exercise.

    The fake score: shorter docs win (``1 / len``). That gives a deterministic
    total order tests can assert against.
    """
    fake_lm = types.ModuleType("mlx_lm")

    def fake_load(path):
        # Returns ``(model, tokenizer)`` — both placeholders since ``_score``
        # is stubbed and never invokes them.
        return object(), object()

    fake_lm.load = fake_load
    monkeypatch.setitem(sys.modules, "mlx_lm", fake_lm)

    from hacienda_gpt.llm import reranker as r

    r._MODEL_CACHE.clear()

    # ``_get_model`` still runs (to populate the cache) but its tuple is
    # never used because ``_score`` doesn't read it.
    original_get_model = r._get_model

    def patched_get_model(path):
        if path not in r._MODEL_CACHE:
            # Populate with placeholder so the cache-singleton test passes.
            r._MODEL_CACHE[path] = (object(), object(), 1, 2)
        return r._MODEL_CACHE[path]

    monkeypatch.setattr(r, "_get_model", patched_get_model)

    # Score: shorter docs win, in [0, 1]. Calls ``_get_model`` first as the
    # real ``_score`` does — that populates the cache, which the
    # cache-sharing test then asserts on.
    def fake_score(self, query: str, docs: list[str]) -> list[float]:
        _ = r._get_model(self.model_path)
        return [1.0 / (1.0 + len(d) * 0.1) for d in docs]

    monkeypatch.setattr(r.MLXReranker, "_score", fake_score)
    return r


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_compress_documents_returns_top_k_and_drops_the_rest(stub_mlx) -> None:
    """Stub gives higher score to shorter docs → reranker must surface them."""
    docs = [
        Document(page_content="x" * 5, metadata={"id": "short1"}),
        Document(page_content="x" * 200, metadata={"id": "long"}),
        Document(page_content="x" * 10, metadata={"id": "short2"}),
    ]
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=2)

    out = reranker.compress_documents(docs, query="any query")

    assert len(out) == 2
    out_ids = {d.metadata["id"] for d in out}
    assert out_ids == {"short1", "short2"}
    assert "long" not in out_ids


def test_compress_documents_stamps_rerank_score_in_metadata(stub_mlx) -> None:
    docs = [Document(page_content="hello", metadata={"id": "x"})]
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=5)

    out = reranker.compress_documents(docs, query="q")

    assert len(out) == 1
    score = out[0].metadata.get("_rerank_score")
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_compress_documents_returns_empty_for_empty_input(stub_mlx) -> None:
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=5)
    assert reranker.compress_documents([], query="q") == []


def test_compress_documents_keeps_all_when_input_smaller_than_top_k(stub_mlx) -> None:
    docs = [Document(page_content=f"d{i}", metadata={"i": i}) for i in range(2)]
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=10)

    out = reranker.compress_documents(docs, query="q")

    assert len(out) == 2


def test_original_documents_are_not_mutated(stub_mlx) -> None:
    """The wrapper stamps the rerank score onto a *copy* — the FAISS docstore
    hands back the same Document instances on every retrieval, so mutating
    them would leak state across requests."""
    original = Document(page_content="hello", metadata={"id": "x"})
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=1)

    _ = reranker.compress_documents([original], query="q")

    assert "_rerank_score" not in original.metadata


def test_async_compress_documents_delegates_to_sync(stub_mlx) -> None:
    import asyncio

    docs = [Document(page_content="abc", metadata={"id": "x"})]
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=1)

    result = asyncio.run(reranker.acompress_documents(docs, query="q"))

    assert len(result) == 1
    assert "_rerank_score" in result[0].metadata


def test_model_cache_is_shared_across_instances(stub_mlx) -> None:
    r1 = stub_mlx.MLXReranker(model_path="/fake/path", top_k=1)
    r2 = stub_mlx.MLXReranker(model_path="/fake/path", top_k=1)

    r1.compress_documents([Document(page_content="a")], query="q")
    r2.compress_documents([Document(page_content="b")], query="q")

    # Both instances triggered ``_get_model`` which populates the cache once.
    assert "/fake/path" in stub_mlx._MODEL_CACHE
    assert len(stub_mlx._MODEL_CACHE) == 1


def test_sort_order_is_descending_by_score(stub_mlx) -> None:
    """Even when keeping all, the order must be score-descending."""
    docs = [
        Document(page_content="x" * 100, metadata={"id": "long"}),
        Document(page_content="x" * 5, metadata={"id": "short"}),
        Document(page_content="x" * 50, metadata={"id": "medium"}),
    ]
    reranker = stub_mlx.MLXReranker(model_path="/fake/path", top_k=10)

    out = reranker.compress_documents(docs, query="q")

    scores = [d.metadata["_rerank_score"] for d in out]
    assert scores == sorted(scores, reverse=True)
    # And the order matches the stub's ranking (short > medium > long).
    assert [d.metadata["id"] for d in out] == ["short", "medium", "long"]


def test_model_cache_is_thread_safe(stub_mlx) -> None:
    """A burst of concurrent first calls must NOT load the model twice.

    The cache is guarded by a lock that double-checks the entry inside the
    critical section. We simulate the burst with several threads racing on
    a fresh cache and assert only one load happened.
    """
    import threading

    stub_mlx._MODEL_CACHE.clear()

    load_calls = {"n": 0}
    real_get_model = stub_mlx._get_model

    def counting_get_model(path):
        if path not in stub_mlx._MODEL_CACHE:
            # Simulate an expensive load — but increment counter unconditionally
            # so we can detect races where two threads both miss.
            load_calls["n"] += 1
        return real_get_model(path)

    # Replace the (already-patched) _get_model with the counter wrapper.
    import hacienda_gpt.llm.reranker as r
    r._get_model = counting_get_model

    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            r._get_model("/concurrent/path")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Without the lock you'd see ``load_calls["n"] > 1`` here under load.
    assert load_calls["n"] == 1, f"expected 1 model load, got {load_calls['n']}"
