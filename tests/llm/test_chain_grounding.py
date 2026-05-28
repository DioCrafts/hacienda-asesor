from __future__ import annotations

from langchain_core.documents import Document

from hacienda_gpt.llm.chain import answer_with_grounding
from hacienda_gpt.llm.grounding import AnswerMode, GroundingGate


class _FakeChain:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.received: dict | None = None

    def invoke(self, inputs: dict) -> dict:
        self.received = inputs
        return self.payload


def test_answer_with_grounding_returns_cited_when_documents_have_metadata() -> None:
    docs = [
        Document(
            page_content="Residencia fiscal en España...",
            metadata={"title": "Residencia fiscal IRPF", "source_url": "https://sede/x"},
        )
    ]
    chain = _FakeChain({"answer": "Si eres residente, declaras IRPF.", "context": docs})

    envelope = answer_with_grounding(
        chain, query="¿Tengo que declarar IRPF?", chat_history=[], gate=GroundingGate(min_citations=1)
    )

    assert envelope.mode is AnswerMode.CITED
    assert envelope.answer.startswith("Si eres residente")
    assert envelope.citations[0].locator == "https://sede/x"
    assert chain.received == {"input": "¿Tengo que declarar IRPF?", "chat_history": []}


def test_answer_with_grounding_abstains_when_chain_returns_no_context() -> None:
    chain = _FakeChain({"answer": "respuesta sin fuentes", "context": []})

    envelope = answer_with_grounding(chain, query="x", gate=GroundingGate(min_citations=1))

    assert envelope.mode is AnswerMode.ABSTAINED
    assert envelope.raw_answer == "respuesta sin fuentes"


def test_answer_with_grounding_abstains_when_model_hedges() -> None:
    docs = [Document(page_content="x", metadata={"title": "t", "source_url": "https://y"})]
    chain = _FakeChain({"answer": "Hmm, no estoy seguro de esto.", "context": docs})

    envelope = answer_with_grounding(chain, query="x", gate=GroundingGate(min_citations=1))

    assert envelope.mode is AnswerMode.ABSTAINED


def test_answer_with_grounding_handles_unexpected_chain_payload() -> None:
    chain = _FakeChain({"foo": "bar"})

    envelope = answer_with_grounding(chain, query="x", gate=GroundingGate(min_citations=1))

    assert envelope.mode is AnswerMode.ABSTAINED
