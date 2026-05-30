from __future__ import annotations

from langchain_core.documents import Document

from hacienda_gpt.llm.grounding import (
    DEFAULT_ABSTAIN_MESSAGE,
    AnswerMode,
    GroundingGate,
    strip_followup_questions,
)


def _doc(
    title: str | None = None, source_url: str | None = None, content: str = "contenido", **extra: object
) -> Document:
    metadata: dict[str, object] = {}
    if title is not None:
        metadata["title"] = title
    if source_url is not None:
        metadata["source_url"] = source_url
    metadata.update(extra)
    return Document(page_content=content, metadata=metadata)


def test_evaluate_returns_cited_when_metadata_is_complete() -> None:
    docs = [
        _doc(
            title="Residencia fiscal IRPF",
            source_url="https://sede.agenciatributaria.gob.es/x",
            document_type="normativa",
        ),
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
    # The canned abstain message is always the prefix; the gate may append a
    # "documentos encontrados" suffix when citations exist.
    assert envelope.answer.startswith(DEFAULT_ABSTAIN_MESSAGE)
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
    docs = [
        duplicated,
        _doc(title="x", source_url="https://y", content="otra parte"),
        _doc(title="z", source_url="https://w"),
    ]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="ok", documents=docs)

    titles = [c.title for c in envelope.citations]
    assert titles == ["x", "z"]


def test_followup_questions_with_hedging_do_not_force_abstention() -> None:
    # The system prompt forces three follow-up questions; one of them mentions
    # "no estoy seguro". A cited answer must NOT be discarded because of it.
    docs = [_doc(title="Residencia fiscal IRPF", source_url="https://sede/x")]
    answer = (
        "Si eres residente fiscal en España, estás obligado a declarar el IRPF "
        "por tu renta mundial.\n\n"
        "**P1**: ¿Qué hago si no estoy seguro de mi residencia fiscal?\n\n"
        "**P2**: ¿Cómo afectan los convenios de doble imposición?\n\n"
        "**P3**: ¿Qué plazos tengo para presentar la declaración?"
    )
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer=answer, documents=docs)

    assert envelope.mode is AnswerMode.CITED
    # The full answer (including the follow-up questions) is preserved.
    assert "no estoy seguro" in envelope.answer


def test_hedging_in_body_still_abstains_even_with_followups() -> None:
    docs = [_doc(title="x", source_url="https://y")]
    answer = (
        "Hmm, no estoy seguro acerca de tu pregunta.\n\n"
        "**P1**: ¿Puedes dar más detalles?\n\n"
        "**P2**: ¿De qué ejercicio fiscal hablamos?\n\n"
        "**P3**: ¿Cuál es tu residencia fiscal?"
    )
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer=answer, documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert envelope.answer.startswith(DEFAULT_ABSTAIN_MESSAGE)


def test_strip_followup_questions_removes_block_but_keeps_isolated_marker() -> None:
    answer = "Cuerpo de la respuesta.\n\n**P1**: a\n\n**P2**: b\n\n**P3**: c"
    assert strip_followup_questions(answer) == "Cuerpo de la respuesta."

    # A single stray marker is not a follow-up block and is left untouched.
    single = "El modelo P1 del formulario se presenta en plazo."
    assert strip_followup_questions(single) == single


def test_citation_snippet_is_truncated() -> None:
    long_text = "palabra " * 100
    docs = [_doc(title="t", source_url="https://y", content=long_text)]
    gate = GroundingGate(min_citations=1, snippet_chars=40)

    envelope = gate.evaluate(answer="ok", documents=docs)

    snippet = envelope.citations[0].snippet or ""
    assert len(snippet) <= 40
    assert snippet.endswith("…")


# --------------------------------------------------------------------------- #
# Stricter hedge detection — issues found in the live QA battery.
# --------------------------------------------------------------------------- #


def test_self_admission_no_information_in_context_triggers_abstain() -> None:
    """The cripto/modelo_721 hallucination: the LLM said 'no está mencionado
    en la legislación consolidada' but the gate did not detect it, so the
    answer was wrongly labelled CITED. Now we honour the LLM's own hedge."""
    answer = (
        "El modelo 721 no está mencionado en la legislación consolidada que "
        "se ha compartido. Es posible que se trate de un error tipográfico."
    )
    docs = [_doc(title="Real Decreto X", source_url="https://aeat/x", content="texto")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer=answer, documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED


def test_self_admission_aunque_en_el_contexto_no_se_detalla_triggers_abstain() -> None:
    """The irpf_beckham false-cite: the LLM admitted 'aunque en el contexto
    proporcionado no se detalla específicamente este régimen' but the gate
    let it through because tangential TEAC citations were retrieved."""
    answer = (
        "Aunque en el contexto proporcionado no se detalla específicamente "
        "este régimen, generalmente los requisitos incluyen lo siguiente..."
    )
    docs = [_doc(title="Doctrina TEAC", source_url="https://aeat/x", content="texto")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer=answer, documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED


def test_self_admission_speculation_about_typo_triggers_abstain() -> None:
    """The most dangerous hallucination: the LLM tells the user they made a
    typo ('podría tratarse de un error tipográfico') instead of admitting
    lack of data. That's misinformation and must be downgraded."""
    answer = "Podría tratarse de un error tipográfico, ya que el modelo 721 no existe."
    docs = [_doc(title="Modelo 720", source_url="https://aeat/720", content="texto")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer=answer, documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED


# --------------------------------------------------------------------------- #
# Informative abstain message — names what we found and what's missing.
# --------------------------------------------------------------------------- #


def test_abstain_message_lists_found_citations() -> None:
    """When abstaining with citations on hand, the message should surface
    them so the user knows what we did look at."""
    docs = [
        _doc(title="Sentencia TC 59/2017", source_url="https://aeat/x"),
        _doc(title="Doctrina TEAC sobre plusvalía", source_url="https://aeat/y"),
    ]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro.", documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert "Documentos relacionados encontrados" in envelope.answer
    assert "Sentencia TC 59/2017" in envelope.answer


def test_abstain_message_flags_missing_modelo_number() -> None:
    """When the user names a specific tax form that's absent from the
    citations, the abstain message must point it out by number."""
    query = "Tengo cripto en el extranjero. ¿Qué es el modelo 721?"
    docs = [_doc(title="Modelo 720 — declaración bienes extranjero", source_url="https://aeat/x", content="bienes en el extranjero")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro.", documents=docs, query=query)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert "modelo 721" in envelope.answer.lower()


def test_abstain_message_flags_missing_ley_reference() -> None:
    """``Ley 7/2012`` in the query but not in any citation → surface it."""
    query = "¿Sigue vigente la Ley 7/2012 sobre modelo 720?"
    docs = [_doc(title="Sentencia TC sobre plusvalía", source_url="https://aeat/x", content="plusvalía municipal")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro.", documents=docs, query=query)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert "Ley 7/2012" in envelope.answer


def test_abstain_message_does_not_flag_when_entity_in_citations() -> None:
    """If the citation snippet contains the entity number, it's NOT missing."""
    query = "¿Cómo afecta la STC 182/2021 a mi liquidación?"
    docs = [
        _doc(
            title="Pleno. Sentencia 182/2021",
            source_url="https://boe/x",
            content="El Tribunal Constitucional, en su sentencia 182/2021, declara...",
        ),
    ]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro.", documents=docs, query=query)

    assert envelope.mode is AnswerMode.ABSTAINED
    # The entity is in the citation → should NOT be listed as missing.
    assert "No encontré información específica sobre" not in envelope.answer


def test_abstain_message_backwards_compatible_when_query_omitted() -> None:
    """Existing callers that don't pass ``query`` still get a sensible
    message — just without the missing-entity stanza."""
    docs = [_doc(title="Algún doc", source_url="https://aeat/x")]
    gate = GroundingGate(min_citations=1)

    envelope = gate.evaluate(answer="Hmm, no estoy seguro.", documents=docs)

    assert envelope.mode is AnswerMode.ABSTAINED
    assert envelope.answer.startswith(DEFAULT_ABSTAIN_MESSAGE)
    # No "missing entity" line when we don't know what the user asked.
    assert "No encontré información específica sobre" not in envelope.answer
