from __future__ import annotations

from langchain_core.documents import Document

from hacienda_gpt.llm.grounding import (
    DEFAULT_ABSTAIN_MESSAGE,
    AnswerMode,
    GroundingGate,
)


def _doc(title: str | None = None, source_url: str | None = None, content: str = "contenido", **extra: object) -> Document:
    metadata: dict[str, object] = {}
    if title is not None:
        metadata["title"] = title
    if source_url is not None:
        metadata["source_url"] = source_url
    metadata.update(extra)
    return Document(page_content=content, metadata=metadata)


def test_evaluate_returns_cited_when_metadata_is_complete() -> None:
    docs = [
        _doc(title="Residencia fiscal IRPF", source_url="https://sede.agenciatributaria.gob.es/x", document_type="normativa"),
        _doc(title="Manual IRPF", source_url="https://sede.agenciatributaria.gob.es/y"),
    ]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Si eres residente, declaras IRPF.", documents=docs)

    assert envelope.mode is AnswerMode.CITED
    assert len(envelope.citations) == 2
    assert envelope.citations[0].title == "Residencia fiscal IRPF"
    assert envelope.citations[0].locator == "https://sede.agenciatributaria.gob.es/x"
    assert envelope.reason is None


def test_evaluate_marks_uncited_when_docs_lack_metadata() -> None:
    docs = [_doc(content="texto sin metadata")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Respuesta sin fuente", documents=docs)

    assert envelope.mode is AnswerMode.UNCITED
    assert envelope.citations == []
    assert envelope.reason is not None


def test_evaluate_abstains_when_no_documents() -> None:
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Respuesta inventada", documents=[])

    assert envelope.mode is AnswerMode.ABSTAINED
    assert envelope.answer == DEFAULT_ABSTAIN_MESSAGE
    assert envelope.raw_answer == "Respuesta inventada"
    assert envelope.reason and "contexto" in envelope.reason.lower()


def test_evaluate_abstains_when_model_emits_doubt_phrase() -> None:
    docs = [_doc(title="x", source_url="https://y")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro acerca de tu pregunta.", documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert envelope.answer == DEFAULT_ABSTAIN_MESSAGE
    assert envelope.raw_answer is not None


def test_evaluate_min_citations_threshold_blocks_partial_metadata() -> None:
    docs = [
        _doc(title="solo titulo"),
        _doc(source_url="https://only-url"),
        _doc(title="Manual", source_url="https://aeat/x"),
    ]
    gate = GroundingGate(min_citations=2)

    envelope = gate.evaluate(answer="respuesta", documents=docs)

    assert envelope.mode is AnswerMode.UNCITED
    assert len(envelope.citations) == 1


def test_evaluate_deduplicates_citations() -> None:
    duplicated = _doc(title="x", source_url="https://y")
    docs = [duplicated, _doc(title="x", source_url="https://y", content="otra parte"), _doc(title="z", source_url="https://w")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="ok", documents=docs)

    titles = [c.title for c in envelope.citations]
    assert titles == ["x", "z"]


def test_citation_snippet_is_truncated() -> None:
    long_text = "palabra " * 100
    docs = [_doc(title="t", source_url="https://y", content=long_text)]
    gate = GroundingGate(min_citations=1, snippet_chars=40)

    envelope = gate.evaluate(answer="ok", documents=docs)

    snippet = envelope.citations[0].snippet or ""
    assert len(snippet) <= 40
    assert snippet.endswith("…")
