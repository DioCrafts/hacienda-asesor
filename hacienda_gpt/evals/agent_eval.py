"""Golden-set evaluation built from DGT binding consultas.

A consulta vinculante is a ready-made test case: a taxpayer describes facts and
asks a question, and the Dirección General de Tributos answers it citing the
applicable law. That gives hundreds of *expert-answered* questions for free.

Two stages, both LLM-driven through the provider layer:

* :func:`build_case` turns one consulta into a self-contained user question plus
  the expected conclusion (and discards consultas that do not make a good test).
* :func:`evaluate_case` runs a system under test on the question — with the
  source consulta hidden from retrieval, so the system has to reach the answer
  from the law and from *other* doctrine rather than by finding the answer key —
  and has a judge compare the answer with the expected conclusion.

The headline number is not accuracy but ``wrong_when_confident``: answers the
system presented as verified/cited that contradict the DGT. For a tax advisor
an abstention is cheap and a confident error is the failure that matters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import random
import re
from typing import Any

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import LLMProvider
from hacienda_gpt.agent.search import CorpusSearcher, SearchFilters

_CONSULTA_ID_RE = re.compile(r"\b([VN]\d{4}-\d{2})\b")
_SKIP_TAGS = frozenset({"script", "style", "head"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# Stage 1 — build the golden set
# --------------------------------------------------------------------------- #


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str  # the consulta number, e.g. "V0058-25"
    question: str
    expected_answer: str
    legal_basis: list[str] = Field(default_factory=list)
    tributo: str | None = None
    consulta_date: str | None = None
    source_file: str | None = None


BUILD_SYSTEM_PROMPT = """\
Preparas casos de prueba para evaluar a un asesor fiscal automático. Recibes el texto íntegro de una consulta \
vinculante de la Dirección General de Tributos (DGT): los hechos que describe el consultante, la cuestión que \
plantea y la contestación de la DGT.

Decide primero si la consulta sirve como caso de prueba (`apta`). Sirve si la DGT da una conclusión clara sobre la \
cuestión (sí/no, qué tributa y cómo, qué requisito se exige, qué plazo o tipo aplica) y esa conclusión se entiende \
sin documentos que no están en el texto. No sirve si la DGT se limita a remitir a otro órgano, a decir que es una \
cuestión de hecho que no puede valorar, o si el texto está incompleto.

Si es apta, redacta:
- `pregunta`: la consulta tal como la escribiría el contribuyente a un asesor, en primera persona y en lenguaje \
natural, con todos los hechos necesarios para responderla (importes, fechas, relación entre las partes, ejercicio) \
y sin mencionar que existe una consulta de la DGT ni su número.
- `respuesta_esperada`: la conclusión de la DGT en 2-5 frases, con las condiciones y cifras de las que depende. \
Solo lo que la DGT afirma; no añadas nada tuyo.
- `normativa`: los preceptos en que se apoya la contestación (p. ej. "artículo 38 Ley 35/2006").
- `tributo`: el impuesto principal (IRPF, IVA, ISD, IS, ITP_AJD, IP, IIVTNU, IAE…).
- `fecha`: la fecha de salida de la consulta en formato AAAA-MM-DD si consta, o cadena vacía.

Si no es apta, deja el resto de campos vacíos y explica por qué en `motivo_descarte`.
"""

BUILD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "apta": {"type": "boolean"},
        "motivo_descarte": {"type": "string"},
        "pregunta": {"type": "string"},
        "respuesta_esperada": {"type": "string"},
        "normativa": {"type": "array", "items": {"type": "string"}},
        "tributo": {"type": "string"},
        "fecha": {"type": "string"},
    },
    "required": ["apta", "motivo_descarte", "pregunta", "respuesta_esperada", "normativa", "tributo", "fecha"],
}


def consulta_id_from_path(path: Path) -> str | None:
    match = _CONSULTA_ID_RE.search(path.stem)
    return match.group(1) if match else None


def sample_consulta_files(dgt_dir: Path, *, sample: int, since_year: int | None, seed: int) -> list[Path]:
    """Deterministic random sample of consulta files, optionally recent-only.

    The two-digit year suffix of the id (``V0058-25`` → 2025) is the filter key;
    DGT consultas start in 1997, so suffixes ≥ 97 are 19xx.
    """
    candidates: list[Path] = []
    for path in sorted(dgt_dir.rglob("*.html")):
        consulta_id = consulta_id_from_path(path)
        if consulta_id is None:
            continue
        suffix = int(consulta_id.rsplit("-", 1)[1])
        year = 1900 + suffix if suffix >= 97 else 2000 + suffix
        if since_year is not None and year < since_year:
            continue
        candidates.append(path)
    random.Random(seed).shuffle(candidates)
    return candidates[:sample]


def build_case(provider: LLMProvider, path: Path) -> GoldenCase | None:
    """Turn one consulta file into a golden case, or None if it is not usable."""
    consulta_id = consulta_id_from_path(path)
    if consulta_id is None:
        return None
    text = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if len(text) < 400:  # an error page or an empty shell, not a consulta
        return None
    data = provider.complete_json(
        system=BUILD_SYSTEM_PROMPT,
        user=f'<consulta numero="{consulta_id}">\n{text}\n</consulta>',
        schema=BUILD_SCHEMA,
        schema_name="caso_de_prueba",
    )
    question = str(data.get("pregunta") or "").strip()
    expected = str(data.get("respuesta_esperada") or "").strip()
    if data.get("apta") is not True or not question or not expected:
        return None
    # The question must not leak the answer key's identity.
    if consulta_id.lower() in question.lower():
        return None
    return GoldenCase(
        case_id=consulta_id,
        question=question,
        expected_answer=expected,
        legal_basis=[str(item).strip() for item in data.get("normativa") or [] if str(item).strip()],
        tributo=str(data.get("tributo") or "").strip() or None,
        consulta_date=str(data.get("fecha") or "").strip() or None,
        source_file=str(path),
    )


def load_golden(path: Path) -> list[GoldenCase]:
    return [
        GoldenCase.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# --------------------------------------------------------------------------- #
# Stage 2 — run a system and judge it
# --------------------------------------------------------------------------- #


class ExcludingSearcher:
    """Hides one consulta from retrieval so the system cannot read the answer key."""

    def __init__(self, inner: CorpusSearcher, consulta_id: str) -> None:
        self._inner = inner
        self._needle = consulta_id.lower()

    def _is_excluded(self, doc: Document) -> bool:
        metadata = doc.metadata or {}
        haystack = " ".join(
            str(metadata.get(key) or "") for key in ("source_url", "source", "source_file", "num_consulta", "title")
        )
        return self._needle in haystack.lower()

    def search(self, query: str, *, k: int, filters: SearchFilters) -> list[Document]:
        # Over-fetch by one so dropping the excluded consulta still fills k.
        docs = self._inner.search(query, k=k + 3, filters=filters)
        return [doc for doc in docs if not self._is_excluded(doc)][:k]


@dataclass(frozen=True)
class SystemAnswer:
    """What a system under test returned for one question."""

    answer: str
    mode: str
    confident: bool  # presented to the user as backed by sources
    abstained: bool
    detail: dict[str, Any]


# A system under test: (golden case) -> SystemAnswer.
SystemUnderTest = Callable[[GoldenCase], SystemAnswer]

JUDGE_SYSTEM_PROMPT = """\
Evalúas la respuesta de un asesor fiscal automático comparándola con el criterio de la Dirección General de \
Tributos (DGT) para el mismo caso. Recibes la pregunta del contribuyente, la conclusión de la DGT (la referencia) y \
la respuesta del asesor. Juzga solo si la conclusión del asesor coincide con la de la DGT, no su estilo ni su \
extensión.

Veredicto:
- "correcta": llega a la misma conclusión que la DGT, con las condiciones esenciales. Puede añadir matices o \
contexto que no contradigan la referencia.
- "parcial": la conclusión principal coincide pero omite una condición, cifra o excepción esencial de la \
referencia, o responde solo a parte de la cuestión.
- "incorrecta": llega a una conclusión distinta o contraria, o da una cifra, plazo o requisito que contradice la \
referencia.
- "sin_respuesta": el asesor se abstiene, pide más datos sin pronunciarse, o no aborda la cuestión.

En `motivo`, una frase que señale la diferencia concreta (o que confirme la coincidencia).
"""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "veredicto": {"type": "string", "enum": ["correcta", "parcial", "incorrecta", "sin_respuesta"]},
        "motivo": {"type": "string"},
    },
    "required": ["veredicto", "motivo"],
}
VERDICTS = ("correcta", "parcial", "incorrecta", "sin_respuesta")


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    question: str
    expected_answer: str
    answer: str
    mode: str
    confident: bool
    abstained: bool
    verdict: str
    judge_reason: str = ""
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


def judge_answer(judge: LLMProvider, case: GoldenCase, answer: str) -> tuple[str, str]:
    data = judge.complete_json(
        system=JUDGE_SYSTEM_PROMPT,
        user=(
            f"<pregunta>\n{case.question}\n</pregunta>\n\n"
            f"<referencia_dgt>\n{case.expected_answer}\n</referencia_dgt>\n\n"
            f"<respuesta_asesor>\n{answer}\n</respuesta_asesor>"
        ),
        schema=JUDGE_SCHEMA,
        schema_name="veredicto_evaluacion",
    )
    verdict = data.get("veredicto")
    if verdict not in VERDICTS:
        raise ValueError(f"veredicto no válido del juez: {verdict!r}")
    return verdict, str(data.get("motivo") or "").strip()


def evaluate_case(system: SystemUnderTest, judge: LLMProvider, case: GoldenCase) -> CaseResult:
    """Run one case end to end. Failures are recorded on the result, not raised,
    so one bad case does not abort a long (and paid) evaluation run."""
    base: dict[str, Any] = {
        "case_id": case.case_id,
        "question": case.question,
        "expected_answer": case.expected_answer,
    }
    try:
        outcome = system(case)
    except Exception as exc:
        return CaseResult(
            **base, answer="", mode="error", confident=False, abstained=False, verdict="error", error=str(exc)
        )
    fields: dict[str, Any] = {
        **base,
        "answer": outcome.answer,
        "mode": outcome.mode,
        "confident": outcome.confident,
        "abstained": outcome.abstained,
        "detail": outcome.detail,
    }
    if outcome.abstained:
        # An explicit abstention needs no judge call.
        return CaseResult(**fields, verdict="sin_respuesta", judge_reason="El sistema se abstuvo.")
    try:
        verdict, reason = judge_answer(judge, case, outcome.answer)
    except Exception as exc:
        return CaseResult(**fields, verdict="error", error=f"juez: {exc}")
    return CaseResult(**fields, verdict=verdict, judge_reason=reason)


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    scored = [result for result in results if result.verdict != "error"]
    total = len(scored)

    def rate(count: int, base: int = total) -> float:
        return round(count / base, 4) if base else 0.0

    counts = {verdict: sum(1 for r in scored if r.verdict == verdict) for verdict in VERDICTS}
    confident = [r for r in scored if r.confident]
    answered = [r for r in scored if r.verdict != "sin_respuesta"]
    return {
        "cases": len(results),
        "scored": total,
        "errors": len(results) - total,
        "verdicts": counts,
        "correct_rate": rate(counts["correcta"]),
        "wrong_rate": rate(counts["incorrecta"]),
        "no_answer_rate": rate(counts["sin_respuesta"]),
        # Of the questions the system chose to answer, how many it got right.
        "correct_when_answered": rate(sum(1 for r in answered if r.verdict == "correcta"), len(answered)),
        # The number that matters: confident answers that contradict the DGT.
        "confident_answers": len(confident),
        "wrong_when_confident": rate(sum(1 for r in confident if r.verdict == "incorrecta"), len(confident)),
        "modes": {mode: sum(1 for r in results if r.mode == mode) for mode in sorted({r.mode for r in results})},
    }


def write_jsonl(path: Path, rows: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(row.model_dump_json() + "\n" for row in rows), encoding="utf-8")


def write_report(path: Path, *, label: str, results: list[CaseResult]) -> dict[str, Any]:
    summary = summarize(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"label": label, "summary": summary, "results": [r.model_dump(mode="json") for r in results]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return summary
