"""Unit tests for the shared embedder factory.

These tests stub the backend packages via ``sys.modules`` so they run without
the heavy ``sentence-transformers`` / ``langchain-*`` stacks installed.
"""

import sys
import types

import pytest

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


def test_qwen3_builds_huggingface_with_query_instruction(monkeypatch):
    captured, fake_cls = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
    monkeypatch.setattr(factory.settings, "EMBEDDING_DEVICE", "cuda")
    monkeypatch.setattr(factory.settings, "EMBEDDING_NORMALIZE", True)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", None)

    emb = factory.create_embeddings("qwen3")

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

    factory.create_embeddings("qwen3")

    assert captured["model_kwargs"] == {"device": "cpu", "truncate_dim": 1024}


def test_non_qwen_hf_model_omits_query_prompt(monkeypatch):
    captured, _ = _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", None)

    factory.create_embeddings("hf", model="intfloat/multilingual-e5-large")

    assert "prompt_name" not in captured["query_encode_kwargs"]


def test_openai_backend(monkeypatch):
    made: dict = {}

    class FakeOpenAIEmbeddings:
        def __init__(self, **kwargs):
            made.update(kwargs)

    mod = types.ModuleType("langchain_openai")
    mod.OpenAIEmbeddings = FakeOpenAIEmbeddings
    monkeypatch.setitem(sys.modules, "langchain_openai", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    emb = factory.create_embeddings("openai")

    assert isinstance(emb, FakeOpenAIEmbeddings)
    assert made["api_key"] == "sk-test"


def test_gpt4all_backend(monkeypatch):
    class FakeGPT4AllEmbeddings:
        def __init__(self, **kwargs):
            pass

    mod = types.ModuleType("langchain_community.embeddings")
    mod.GPT4AllEmbeddings = FakeGPT4AllEmbeddings
    monkeypatch.setitem(sys.modules, "langchain_community.embeddings", mod)

    assert isinstance(factory.create_embeddings("gpt4all"), FakeGPT4AllEmbeddings)


def test_embedder_name_is_case_insensitive_and_trimmed(monkeypatch):
    _install_fake_hf(monkeypatch)
    monkeypatch.setattr(factory.settings, "EMBEDDING_DIM", None)

    # Should resolve to the HuggingFace backend without raising.
    factory.create_embeddings("  QWEN3 ")


def test_unknown_embedder_raises():
    with pytest.raises(ValueError, match="Unknown EMBEDDER"):
        factory.create_embeddings("does-not-exist")
