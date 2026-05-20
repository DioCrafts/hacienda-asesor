from unittest.mock import MagicMock

import pytest

from hacienda_gpt.llm import chain


def test_create_retriever_raises_when_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chain, "FAISS_TRUSTED_INDEX", False)

    with pytest.raises(RuntimeError, match="Refusing to load FAISS index"):
        chain._create_retriever(embeddings=MagicMock(), llm=MagicMock())


def test_create_retriever_builds_compressed_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_faiss = MagicMock()
    fake_base_retriever = MagicMock(name="base_retriever")
    fake_faiss.as_retriever.return_value = fake_base_retriever

    fake_multi_query_retriever = MagicMock(name="multi_query_retriever")
    fake_embeddings_filter = MagicMock(name="embeddings_filter")
    fake_compressed_retriever = MagicMock(name="compressed_retriever")

    monkeypatch.setattr(chain, "FAISS_TRUSTED_INDEX", True)
    monkeypatch.setattr(chain.FAISS, "load_local", MagicMock(return_value=fake_faiss))
    monkeypatch.setattr(chain.MultiQueryRetriever, "from_llm", MagicMock(return_value=fake_multi_query_retriever))
    monkeypatch.setattr(chain, "EmbeddingsFilter", MagicMock(return_value=fake_embeddings_filter))
    monkeypatch.setattr(chain, "ContextualCompressionRetriever", MagicMock(return_value=fake_compressed_retriever))

    result = chain._create_retriever(embeddings=MagicMock(), llm=MagicMock())

    chain.FAISS.load_local.assert_called_once()
    fake_faiss.as_retriever.assert_called_once_with(search_kwargs={"k": chain.TOP_K})
    chain.MultiQueryRetriever.from_llm.assert_called_once()
    chain.EmbeddingsFilter.assert_called_once()
    chain.ContextualCompressionRetriever.assert_called_once_with(
        base_retriever=fake_multi_query_retriever,
        base_compressor=fake_embeddings_filter,
    )
    assert hasattr(result, "inner")
    assert result.inner is fake_compressed_retriever
