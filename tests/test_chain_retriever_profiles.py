from unittest.mock import MagicMock

from hacienda_gpt.llm import chain


def test_create_retriever_applies_profile_filter_and_threshold(monkeypatch) -> None:
    fake_faiss = MagicMock()
    fake_base = MagicMock()
    fake_faiss.as_retriever.return_value = fake_base

    monkeypatch.setattr(chain, "FAISS_TRUSTED_INDEX", True)
    monkeypatch.setattr(chain.FAISS, "load_local", MagicMock(return_value=fake_faiss))
    monkeypatch.setattr(chain.MultiQueryRetriever, "from_llm", MagicMock(return_value=MagicMock()))

    emb_filter = MagicMock()
    monkeypatch.setattr(chain, "EmbeddingsFilter", MagicMock(return_value=emb_filter))
    monkeypatch.setattr(chain, "ContextualCompressionRetriever", MagicMock(return_value=MagicMock()))

    chain._create_retriever(MagicMock(), MagicMock(), profile_name="decision", fiscal_year=2025, metadata_filter={"scope": "nacional"})

    fake_faiss.as_retriever.assert_called_once()
    kwargs = fake_faiss.as_retriever.call_args.kwargs["search_kwargs"]
    assert kwargs["filter"]["fiscal_year"] == 2025
