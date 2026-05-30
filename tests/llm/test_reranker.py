"""Unit tests for the MLX reranker wrapper.

The real ``mlx_lm`` model load is expensive (1.1 GB on disk) and Apple-Silicon
only, so we stub the loader via ``sys.modules``. The integration-level
verification (numerical equivalence vs PyTorch, latency) runs by hand on the
target hardware; see ``/tmp/rerank-test/score.py`` in the dev notes.

The stubs below are intentionally minimal: just enough surface for the
wrapper's scoring path to execute deterministically, so tests assert
behaviour (sort order, top-K, score stamping) without touching real MLX.
"""

from __future__ import annotations

import math
import sys
import types
from typing import Any

import pytest
from langchain_core.documents import Document


# --------------------------------------------------------------------------- #
# Minimal stubs for mlx_lm.load() + mlx.core surface
# --------------------------------------------------------------------------- #

YES_ID, NO_ID = 1, 2


class _Scalar:
    def __init__(self, v: float) -> None:
        self._v = v

    def item(self) -> float:
        return self._v


class _RowLogits:
    """Indexed by token id → returns a scalar. Shorter inputs score higher."""

    def __init__(self, length: int) -> None:
        self._length = length

    def __getitem__(self, token_id: int) -> _Scalar:
        if token_id == YES_ID:
            return _Scalar(10.0 - self._length * 0.1)
        if token_id == NO_ID:
            return _Scalar(0.0)
        return _Scalar(0.0)


class _ModelOutput:
    """Indexed as ``logits[0, -1, :]`` → returns a row of logits."""

    def __init__(self, length: int) -> None:
        self._length = length

    def __getitem__(self, idx: Any) -> _RowLogits:
        return _RowLogits(self._length)


class _FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, x: "_Batched") -> _ModelOutput:
        self.calls += 1
        return _ModelOutput(x.shape[1])


class _FakeTokenizer:
    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        if text == "yes":
            return [YES_ID]
        if text == "no":
            return [NO_ID]
        # Length proportional to input — lets us drive scoring deterministically.
        return list(range(max(1, len(text))))


class _FakeArray:
    """``mx.array(ids)[None, :]`` returns this; only ``.shape`` is read."""

    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    def __getitem__(self, idx: Any) -> "_Batched":
        return _Batched(self._ids)


class _Batched:
    def __init__(self, ids: list[int]) -> None:
        self.shape = (1, len(ids))


class _Stacked:
    def __init__(self, scalars: list[_Scalar]) -> None:
        self.scalars = scalars


class _SoftmaxResult:
    def __init__(self, scalars: list[_Scalar]) -> None:
        values = [s.item() for s in scalars]
        peak = max(values)
        exps = [math.exp(v - peak) for v in values]
        total = sum(exps)
        self._probs = [e / total for e in exps]

    def __getitem__(self, idx: int) -> _Scalar:
        return _Scalar(self._probs[idx])


def _mx_array(ids: list[int]) -> _FakeArray:
    return _FakeArray(ids)


def _mx_stack(items: list[_Scalar]) -> _Stacked:
    return _Stacked(items)


def _mx_softmax(stacked: _Stacked) -> _SoftmaxResult:
    return _SoftmaxResult(stacked.scalars)


@pytest.fixture
def stub_mlx(monkeypatch):
    """Stub ``mlx.core`` + ``mlx_lm.load`` and reset the reranker module cache."""
    fake_mx = types.ModuleType("mlx")
    fake_mx_core = types.ModuleType("mlx.core")
    fake_mx_core.array = _mx_array
    fake_mx_core.stack = _mx_stack
    fake_mx_core.softmax = _mx_softmax
    fake_mx.core = fake_mx_core
    monkeypatch.setitem(sys.modules, "mlx", fake_mx)
    monkeypatch.setitem(sys.modules, "mlx.core", fake_mx_core)

    fake_lm = types.ModuleType("mlx_lm")
    fake_lm.load = lambda path: (_FakeModel(), _FakeTokenizer())
    monkeypatch.setitem(sys.modules, "mlx_lm", fake_lm)

    from hacienda_gpt.llm import reranker as r

    r._MODEL_CACHE.clear()
    return r


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_compress_documents_returns_top_k_and_drops_the_rest(stub_mlx) -> None:
    """Fake model scores short docs higher → reranker must surface them."""
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
    # And the order matches the synthetic ranking (short > medium > long).
    assert [d.metadata["id"] for d in out] == ["short", "medium", "long"]
