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
    # Self-admissions that the answer is NOT grounded in the retrieved context.
    # Caught the modelo_721 false-cite where the LLM said "no está mencionado en
    # la legislación consolidada" but the gate marked the answer as cited
    # because tangential citations existed. The patterns below downgrade those
    # responses to ABSTAINED so the LLM's own hedge is honoured.
    r"aunque\s+(?:en\s+)?el\s+contexto\s+(?:proporcionado\s+)?no\s+(?:se\s+detalla|se\s+menciona|incluye)",
    r"no\s+(?:hay|aparece|figura|consta)\s+(?:información|informacion|datos|menci[oó]n)\s+(?:específica|especifica|en\s+el\s+contexto)",
    r"no\s+est[áa]\s+mencion(?:ad[oa]|ar)\s+en\s+(?:la\s+legislaci[oó]n|el\s+contexto|los\s+documentos)",
    r"no\s+se\s+detalla\s+(?:específicamente|especificamente)?\s*(?:en|sobre|este)",
    # Defensive: the LLM speculates that the user made a mistake (typo / confusion)
    # instead of admitting lack of data. Caught the modelo_721 hallucination
    # where it said "es posible que se trate de un error tipográfico o confusión
    # con el modelo 720" — a misleading invention for a real form (modelo 721).
    r"(?:es\s+posible|podr[ií]a\s+tratarse|tal\s+vez|quiz[áa]s?)\s+(?:de\s+)?(?:que\s+se\s+trate\s+de\s+)?un\s+error\s+(?:tipogr[áa]fico|de\s+(?:escritura|transcripci[oó]n))",
    r"(?:podr[ií]a\s+ser|es\s+posible\s+que\s+sea)\s+(?:una\s+)?confusi[oó]n\s+con",
)


# Patterns for named regulatory entities a user might cite by number. When the
# user mentions one of these and it is absent from the retrieved citations, we
# surface the specific gap in the abstain message (instead of a canned "no
# encontré información"). The (label, regex) pairs feed both detection and the
# human-readable rendering.
_ENTITY_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("modelo", re.compile(r"\bmodelo\s+(\d{3,4})\b", re.IGNORECASE)),
    ("Ley", re.compile(r"\bley\s+(\d{1,3}/\d{4})\b", re.IGNORECASE)),
    ("Real Decreto-ley", re.compile(r"\b(?:real\s+decreto-ley|rdl)\s+(\d{1,3}/\d{4})\b", re.IGNORECASE)),
    ("Real Decreto", re.compile(r"\b(?:real\s+decreto|rd)\s+(\d{1,4}/\d{4})\b", re.IGNORECASE)),
    ("STC", re.compile(r"\bstc\s+(\d{1,4}/\d{4})\b", re.IGNORECASE)),
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

    def evaluate(
        self,
        answer: str,
        documents: Sequence[Any],
        *,
        query: str = "",
    ) -> AnswerEnvelope:
        """Apply the abstention policy and wrap the result in an envelope.

        ``query`` (optional) is the original user question. When supplied, the
        abstain message names the specific regulatory references the user asked
        about that are absent from the retrieved documents — turning the
        canned "no tengo información" into actionable feedback. Backwards
        compatible: existing callers that omit ``query`` keep the old terse
        canned message.
        """
        citations = self._collect_citations(documents)
        clean_answer = (answer or "").strip()
        # Evaluate hedging only on the answer body, never on the appended
        # follow-up questions (see `strip_followup_questions`).
        answer_body = strip_followup_questions(clean_answer)

        if self._matches_abstain_pattern(answer_body) or not documents:
            return AnswerEnvelope(
                answer=self._build_abstain_message(query, citations),
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

    def _build_abstain_message(self, query: str, citations: list["Citation"]) -> str:
        """Compose an abstain message that names what we DID find and what's
        missing. Falls back to the canned message when there's nothing useful
        to add (no query, no citations, no detected gap).
        """
        parts: list[str] = [self.abstain_message]

        if citations:
            found_titles: list[str] = []
            seen: set[str] = set()
            for c in citations:
                label = (c.title or "").strip().rstrip(":,.;")
                if not label:
                    continue
                if len(label) > 80:
                    label = label[:77].rstrip() + "…"
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                found_titles.append(label)
                if len(found_titles) == 3:
                    break
            if found_titles:
                parts.append(
                    "Documentos relacionados encontrados (pero insuficientes para responder): "
                    + "; ".join(found_titles)
                    + "."
                )

        if query:
            missing = _detect_missing_entities(query, citations)
            if missing:
                parts.append(
                    "No encontré información específica sobre: "
                    + ", ".join(missing)
                    + ". Verifica estas referencias con la Agencia Tributaria o un asesor fiscal."
                )

        return " ".join(parts)

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


def _detect_missing_entities(query: str, citations: Sequence[Citation]) -> list[str]:
    """Identify named regulatory references in the query that don't appear in
    any retrieved citation.

    The detection is intentionally narrow: only structured identifiers
    (``modelo 721``, ``Ley 7/2012``, ``STC 182/2021``, ``Real Decreto 249/2023``)
    where a missing match is strong evidence of a coverage gap. Free-form
    topic names (``IRPF``, ``vivienda habitual``) aren't flagged here because
    they appear in many tangential documents and the false-positive rate is
    too high. The output is rendered to the user, so it must be **specific**
    enough to be actionable.
    """
    if not query:
        return []
    haystack = " ".join(
        (c.title or "") + " " + (c.snippet or "") for c in citations
    )
    missing: list[str] = []
    seen: set[str] = set()
    for label, pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(query):
            number = match.group(1)
            entity = f"{label} {number}"
            entity_key = entity.lower()
            if entity_key in seen:
                continue
            seen.add(entity_key)
            if not _entity_in_haystack(number, haystack):
                missing.append(entity)
    return missing


def _entity_in_haystack(entity_number: str, haystack: str) -> bool:
    """Whether the identifier (e.g. ``720`` or ``7/2012``) appears in haystack.

    For slash-separated identifiers (``N/AAAA``) we tolerate optional
    whitespace around the slash because OCR / HTML conversion sometimes
    introduces it. For bare numbers we require a word boundary so e.g.
    ``721`` doesn't match inside ``7210`` or ``BOE-A-2018-7211``.
    """
    if "/" in entity_number:
        num, year = entity_number.split("/", 1)
        pattern = rf"\b{re.escape(num)}\s*/\s*{re.escape(year)}\b"
    else:
        pattern = rf"\b{re.escape(entity_number)}\b"
    return bool(re.search(pattern, haystack, re.IGNORECASE))
