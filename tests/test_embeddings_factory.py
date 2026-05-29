"""Unit tests for the embedder factory (local Qwen3 / sentence-transformers).

Stubs ``langchain_huggingface`` via ``sys.modules`` so they run without the
heavy sentence-transformers stack installed.
"""

import sys
import types

from hacienda_gpt.llm import embeddings as factory


def _install_fake_hf(monkeypatch):
    """Replace langchain_huggingface.HuggingFaceEmbeddings with a recorder."""
    captured: dict = {}

    class FakeHFE:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    mod = types.ModuleType("langchain_huggingface")
    mod.HuggingFaceEmbeddings = FakeHFE
    monkeypatch.setitem(sys.modules, "langchain_huggingface", mod)
    return captured, FakeHFE


def test_builds_huggingface_with_query_instruction(monkeypatch):
    captured, fake_cls = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
    monkeypatch.setattr(factory.settings, "EMBEDDING_DEVICE", "cuda")
    monkeypatch.setattr(factory.settings, "EMBEDDING_NORMALIZE", True)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", None)

    emb = factory.create_embeddings()

    assert isinstance(emb, fake_cls)
    assert captured["model_name"] == "Qwen/Qwen3-Embedding-8B"
    assert captured["model_kwargs"] == {"device": "cuda"}
    assert captured["encode_kwargs"] == {"normalize_embeddings": True}
    # Queries get the Qwen instruction prompt; documents do not.
    assert captured["query_encode_kwargs"]["prompt_name"] == "query"
    assert captured["query_encode_kwargs"]["normalize_embeddings"] is True


def test_embedding_dim_enables_mrl_truncation(monkeypatch):
    captured, _ = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
    monkeypatch.setattr(factory.settings, "EMBEDDING_DEVICE", "cpu")
    monkeypatch.setattr(factory.settings, "EMBEDDING_NORMALIZE", True)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", "1024")

    factory.create_embeddings()

    assert captured["model_kwargs"] == {"device": "cpu", "truncate_dim": 1024}


def test_model_override_non_qwen_omits_query_prompt(monkeypatch):
    captured, _ = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", None)

    factory.create_embeddings(model="intfloat/multilingual-e5-large")

    assert captured["model_name"] == "intfloat/multilingual-e5-large"
    assert "prompt_name" not in captured["query_encode_kwargs"]
