"""Grounding gate: enforces citation-backed answers and forces abstention.

In a fiscal advisor the tolerable hallucination rate is close to zero, so
the chain output is wrapped in an :class:`AnswerEnvelope` whose mode is
decided by a :class:`GroundingGate`:

* ``CITED``     – at least ``min_citations`` retrieved documents expose a
  meaningful citable identifier (title, source_url, …). The answer is
  shown as-is with its citations.
* ``UNCITED``   – the chain produced context but no document is citable.
  The answer is surfaced with a warning banner: it must not be relied on.
* ``ABSTAINED`` – there is no usable context, or the model itself emitted
  a hedging phrase. The original answer is replaced by a canned message.

The gate is intentionally conservative: when in doubt, abstain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ABSTAIN_MESSAGE = (
    "No tengo información normativa suficiente para responder con seguridad. "
    "Te recomiendo consultar directamente con la Agencia Tributaria o un asesor fiscal."
)

DEFAULT_ABSTAIN_PATTERNS: tuple[str, ...] = (
    r"no\s+estoy\s+seguro",
    r"no\s+tengo\s+(?:información|informacion)\s+suficiente",
    r"hmm,?\s+no\s+estoy\s+seguro",
    r"no\s+puedo\s+responder",
    r"no\s+dispongo\s+de\s+(?:información|informacion)",
)

# The QA system prompt mandates three follow-up questions rendered as bold
# markers (P1, P2, P3) appended after the answer body. Those questions are not
# the model's own answer and routinely contain hedging-sounding wording
# ("¿Qué hago si no estoy seguro de mi residencia fiscal?"). Scanning them for
# abstention phrases turned well-grounded, cited answers into false
# abstentions, so the block is stripped before the hedge check.
_FOLLOWUP_MARKER_RE = re.compile(r"(?im)^\s*\*{0,2}\s*P([123])\b")


def strip_followup_questions(answer: str) -> str:
    """Return the answer body with the trailing P1/P2/P3 block removed.

    Only strips when at least two follow-up markers are present (the prompt
    always emits three), so an isolated ``P1`` appearing inside legitimate
    body text is left untouched.
    """
    markers = list(_FOLLOWUP_MARKER_RE.finditer(answer))
    if len(markers) < 2:
        return answer
    return answer[: markers[0].start()].rstrip()


class AnswerMode(str, Enum):
    CITED = "cited"
    UNCITED = "uncited"
    ABSTAINED = "abstained"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    locator: str
    document_type: str | None = None
    section: str | None = None
    snippet: str | None = None


class AnswerEnvelope(BaseModel):
    """Wraps a raw LLM answer with a grounding verdict and citations."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    mode: AnswerMode
    citations: list[Citation] = Field(default_factory=list)
    raw_answer: str | None = None
    reason: str | None = None
    min_citations_required: int


@dataclass(frozen=True)
class GroundingGate:
    """Apply the abstention policy to a retrieval-augmented response."""

    min_citations: int = 1
    snippet_chars: int = 240
    abstain_message: str = DEFAULT_ABSTAIN_MESSAGE
    abstain_patterns: tuple[str, ...] = DEFAULT_ABSTAIN_PATTERNS

    def evaluate(self, answer: str, documents: Sequence[Any]) -> AnswerEnvelope:
        citations = self._collect_citations(documents)
        clean_answer = (answer or "").strip()
        # Evaluate hedging only on the answer body, never on the appended
        # follow-up questions (see `strip_followup_questions`).
        answer_body = strip_followup_questions(clean_answer)

        if self._matches_abstain_pattern(answer_body) or not documents:
            return AnswerEnvelope(
                answer=self.abstain_message,
                mode=AnswerMode.ABSTAINED,
                citations=citations,
                raw_answer=clean_answer or None,
                reason=self._abstain_reason(answer_body, documents),
                min_citations_required=self.min_citations,
            )

        if len(citations) >= self.min_citations:
            return AnswerEnvelope(
                answer=clean_answer,
                mode=AnswerMode.CITED,
                citations=citations,
                raw_answer=None,
                reason=None,
                min_citations_required=self.min_citations,
            )

        return AnswerEnvelope(
            answer=clean_answer,
            mode=AnswerMode.UNCITED,
            citations=citations,
            raw_answer=None,
            reason=(
                f"Contexto recuperado sin metadata citable suficiente "
                f"(esperadas: {self.min_citations}, encontradas: {len(citations)})."
            ),
            min_citations_required=self.min_citations,
        )

    def _matches_abstain_pattern(self, answer: str) -> bool:
        if not answer:
            return False
        lowered = answer.lower()
        return any(re.search(pattern, lowered) for pattern in self.abstain_patterns)

    def _abstain_reason(self, answer: str, documents: Sequence[Any]) -> str:
        if not documents:
            return "No se recuperó contexto normativo para la consulta."
        if self._matches_abstain_pattern(answer):
            return "El modelo expresó incertidumbre explícita en su respuesta."
        return "Abstención por política de grounding."

    def _collect_citations(self, documents: Sequence[Any]) -> list[Citation]:
        seen: set[tuple[str, str]] = set()
        citations: list[Citation] = []
        for doc in documents:
            citation = self._document_to_citation(doc)
            if citation is None:
                continue
            key = (citation.title, citation.locator)
            if key in seen:
                continue
            seen.add(key)
            citations.append(citation)
        return citations

    def _document_to_citation(self, doc: Any) -> Citation | None:
        metadata = getattr(doc, "metadata", None) or {}
        title = self._first_non_empty(metadata, ("title", "section"))
        locator = self._first_non_empty(metadata, ("source_url", "source", "locator"))
        if not title or not locator:
            return None
        snippet = self._truncate(getattr(doc, "page_content", "") or "")
        return Citation(
            title=title,
            locator=locator,
            document_type=metadata.get("document_type"),
            section=metadata.get("section"),
            snippet=snippet or None,
        )

    @staticmethod
    def _first_non_empty(metadata: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _truncate(self, text: str) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= self.snippet_chars:
            return cleaned
        return cleaned[: self.snippet_chars - 1].rstrip() + "…"
