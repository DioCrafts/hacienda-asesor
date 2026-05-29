from unittest.mock import MagicMock

from langchain_core.retrievers import BaseRetriever

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
    monkeypatch.setattr(chain, "ContextualCompressionRetriever", MagicMock(return_value=MagicMock(spec=BaseRetriever)))

    chain._create_retriever(MagicMock(), MagicMock(), profile_name="decision", metadata_filter={"scope": "nacional"})

    fake_faiss.as_retriever.assert_called_once()
    kwargs = fake_faiss.as_retriever.call_args.kwargs["search_kwargs"]
    flt = kwargs["filter"]
    # Best-effort predicate: an exact match passes, an absent field is kept (so
    # documents lacking the key survive the filter), an explicit mismatch fails.
    assert callable(flt)
    assert flt({"scope": "nacional"}) is True
    assert flt({"document_type": "manual"}) is True
    assert flt({"scope": "regional"}) is False


def _stub_chain_builders(monkeypatch) -> dict:
    """Stub every external builder so create_openai_chain runs offline and
    record the profile_name forwarded to the retriever factory."""
    captured: dict = {}

    def fake_create_retriever(embeddings, llm, *, profile_name="decision", metadata_filter=None):
        captured["profile_name"] = profile_name
        return MagicMock(spec=BaseRetriever)

    monkeypatch.setattr(chain, "_create_retriever", fake_create_retriever)
    monkeypatch.setattr(chain, "ChatOpenAI", MagicMock())
    monkeypatch.setattr(chain, "create_embeddings", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(chain, "create_history_aware_retriever", MagicMock(return_value=object()))
    monkeypatch.setattr(chain, "create_stuff_documents_chain", MagicMock(return_value=object()))
    monkeypatch.setattr(chain, "create_retrieval_chain", MagicMock(return_value="CHAIN"))
    monkeypatch.setattr(chain, "ChatPromptTemplate", MagicMock())
    monkeypatch.setattr(chain, "MessagesPlaceholder", MagicMock())
    return captured


def test_create_openai_chain_uses_explain_profile_by_default(monkeypatch) -> None:
    captured = _stub_chain_builders(monkeypatch)

    result = chain.create_openai_chain(openai_api_key="sk-test")

    # /qa and the Streamlit chat are explanatory Q&A → higher-recall profile.
    assert captured["profile_name"] == "explain"
    assert result == "CHAIN"


def test_create_openai_chain_forwards_explicit_profile(monkeypatch) -> None:
    captured = _stub_chain_builders(monkeypatch)

    chain.create_openai_chain(openai_api_key="sk-test", profile_name="decision")

    assert captured["profile_name"] == "decision"
