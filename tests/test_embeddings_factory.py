"""Unit tests for the MLX embedder factory.

The real ``mlx_embeddings`` package is only installable on Apple Silicon, so
these tests stub it via ``sys.modules`` to keep the factory's wiring testable
on any platform. Behaviour checks (numerical equivalence, throughput) live in
the integration suite that runs only on Apple Silicon.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from hacienda_gpt.llm import embeddings as factory


class _FakeArray:
    """Stand-in for an ``mlx.core.array`` — just enough to support
    ``.tolist()`` on the embedder output path."""

    def __init__(self, data: list[list[float]]) -> None:
        self._data = data

    def tolist(self) -> list[list[float]]:
        return self._data


class _FakeOutput:
    def __init__(self, vecs: list[list[float]]) -> None:
        self.text_embeds = _FakeArray(vecs)


class _FakeModel:
    """Captures the calls so tests can assert what the factory passed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, input_ids, attention_mask):
        self.calls.append({"input_ids": input_ids, "attention_mask": attention_mask})
        # Pretend each row in the batch becomes a 4-dim vector — value irrelevant.
        batch_size = len(input_ids) if hasattr(input_ids, "__len__") else 1
        return _FakeOutput([[0.1, 0.2, 0.3, 0.4]] * batch_size)


class _FakeTokenizer:
    """Records inputs so we can assert prefixes/batching."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, batch, *, padding, truncation, max_length, return_tensors):
        # Synthetic token ids: one row per input, fixed length 4.
        n = len(batch)
        ids = [[1, 2, 3, 4]] * n
        mask = [[1, 1, 1, 1]] * n
        self.calls.append(
            {
                "batch": list(batch),
                "padding": padding,
                "truncation": truncation,
                "max_length": max_length,
                "return_tensors": return_tensors,
            }
        )
        return {"input_ids": ids, "attention_mask": mask}


def _install_fake_mlx(monkeypatch):
    """Replace ``mlx_embeddings.load`` with a stub returning fake (model, tokenizer)."""
    fake_model = _FakeModel()
    fake_tok = _FakeTokenizer()

    def fake_load(path):
        return fake_model, fake_tok

    mod = types.ModuleType("mlx_embeddings")
    mod.load = fake_load
    monkeypatch.setitem(sys.modules, "mlx_embeddings", mod)
    return fake_model, fake_tok


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """Minimal on-disk model directory with the Qwen3 prompt config."""
    d = tmp_path / "model"
    d.mkdir()
    (d / "config_sentence_transformers.json").write_text(
        json.dumps({"prompts": {"query": "Instruct: test\nQuery:", "document": ""}}),
        encoding="utf-8",
    )
    return d


def test_factory_returns_mlx_embedder(monkeypatch, model_dir: Path) -> None:
    _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(model_dir))
    monkeypatch.setattr(factory.settings, "EMBEDDING_BATCH_SIZE", 8)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MAX_SEQ_LENGTH", 512)

    emb = factory.create_embeddings()

    assert isinstance(emb, factory.MLXEmbeddings)


def test_embed_documents_passes_through_tokenizer(monkeypatch, model_dir: Path) -> None:
    _, tokenizer = _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(model_dir))
    monkeypatch.setattr(factory.settings, "EMBEDDING_BATCH_SIZE", 8)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MAX_SEQ_LENGTH", 256)

    emb = factory.create_embeddings()
    vecs = emb.embed_documents(["foo", "bar"])

    # 2 inputs → 2 vectors, deterministic from the fake model.
    assert len(vecs) == 2
    assert len(vecs[0]) == 4
    # Documents must NOT receive the query instruction prefix.
    assert tokenizer.calls[0]["batch"] == ["foo", "bar"]
    assert tokenizer.calls[0]["padding"] is True
    assert tokenizer.calls[0]["truncation"] is True
    assert tokenizer.calls[0]["max_length"] == 256
    assert tokenizer.calls[0]["return_tensors"] == "mlx"


def test_embed_query_prepends_instruction_prefix(monkeypatch, model_dir: Path) -> None:
    _, tokenizer = _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(model_dir))

    emb = factory.create_embeddings()
    vec = emb.embed_query("¿es deducible el coche?")

    assert len(vec) == 4
    # The query string should be wrapped with the prompt from the config.
    sent_to_tokenizer = tokenizer.calls[0]["batch"]
    assert sent_to_tokenizer == ["Instruct: test\nQuery: ¿es deducible el coche?"]


def test_missing_prompt_config_disables_prefix_gracefully(monkeypatch, tmp_path: Path) -> None:
    """No ``config_sentence_transformers.json`` → no prefix, no crash."""
    _, tokenizer = _install_fake_mlx(monkeypatch)
    d = tmp_path / "no_prompts"
    d.mkdir()
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(d))

    emb = factory.create_embeddings()
    emb.embed_query("¿deducción?")

    # Query goes through unprefixed.
    assert tokenizer.calls[0]["batch"] == ["¿deducción?"]


def test_batches_respect_batch_size(monkeypatch, model_dir: Path) -> None:
    """5 inputs / batch_size=2 → 3 tokenizer calls of sizes [2, 2, 1]."""
    _, tokenizer = _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(model_dir))
    monkeypatch.setattr(factory.settings, "EMBEDDING_BATCH_SIZE", 2)

    emb = factory.create_embeddings()
    vecs = emb.embed_documents(["a", "b", "c", "d", "e"])

    assert len(vecs) == 5
    sizes = [len(call["batch"]) for call in tokenizer.calls]
    assert sizes == [2, 2, 1]


def test_missing_model_directory_raises(monkeypatch, tmp_path: Path) -> None:
    """Loud failure when EMBEDDING_MODEL points at a path that doesn't exist."""
    _install_fake_mlx(monkeypatch)
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(missing))

    with pytest.raises(FileNotFoundError, match="MLX model directory not found"):
        factory.create_embeddings()


def test_embed_documents_empty_list_returns_empty(monkeypatch, model_dir: Path) -> None:
    """No texts → no work, no tokenizer call."""
    _, tokenizer = _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", str(model_dir))

    emb = factory.create_embeddings()
    assert emb.embed_documents([]) == []
    assert tokenizer.calls == []


def test_model_override_argument(monkeypatch, model_dir: Path) -> None:
    """The factory's ``model=`` arg overrides settings.EMBEDDING_MODEL."""
    _install_fake_mlx(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", "/wrong/path")

    # Pass the real path — should not consult settings.
    emb = factory.create_embeddings(model=str(model_dir))
    assert isinstance(emb, factory.MLXEmbeddings)
