"""Claim-level verification of an agent answer against its cited fragments.

This replaces the regex grounding gate for the agent path. The gate marked an
answer ``cited`` whenever retrieval returned documents with metadata; it never
checked that the answer said what those documents say. Here a separate model
call reads each normative claim next to the fragment it cites and rules on it.
The verifier sees only the question, the answer and the cited text — none of
the agent's reasoning — so it cannot be talked into agreeing.
"""

from __future__ import annotations

from enum import Enum
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.evidence import Fragment
from hacienda_gpt.agent.prompts import VERIFIER_SCHEMA, VERIFIER_SYSTEM_PROMPT
from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError

logger = logging.getLogger(__name__)


class ResponseType(str, Enum):
    ANSWER = "respuesta"
    CLARIFICATION = "pregunta_aclaratoria"
    ABSTENTION = "abstencion"


class Verdict(str, Enum):
    SUPPORTED = "respaldada"
    PARTIAL = "parcial"
    UNSUPPORTED = "no_respaldada"


class ClaimCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    fragment_ids: list[str] = Field(default_factory=list)
    verdict: Verdict
    reason: str = ""


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_type: ResponseType = ResponseType.ANSWER
    claims: list[ClaimCheck] = Field(default_factory=list)
    # The answer tells the user the question is unsettled / sources disagree.
    declares_no_settled_criterion: bool = False
    # Set when verification itself could not run; the answer is then reported
    # as unverified — a failed check must never read as a passed one.
    error: str | None = None

    @property
    def problems(self) -> list[ClaimCheck]:
        return [claim for claim in self.claims if claim.verdict is not Verdict.SUPPORTED]

    @property
    def supported(self) -> list[ClaimCheck]:
        return [claim for claim in self.claims if claim.verdict is Verdict.SUPPORTED]


def _build_user_message(question: str, answer: str, fragments: list[Fragment]) -> str:
    rendered = "\n\n".join(fragment.render() for fragment in fragments) or "(la respuesta no cita ningún fragmento)"
    return (
        f"<pregunta>\n{question}\n</pregunta>\n\n"
        f"<respuesta>\n{answer}\n</respuesta>\n\n"
        f"<fragmentos_citados>\n{rendered}\n</fragmentos_citados>"
    )


def _parse_report(data: dict[str, Any], known_ids: set[str]) -> VerificationReport:
    try:
        response_type = ResponseType(data.get("tipo_respuesta"))
    except ValueError:
        response_type = ResponseType.ANSWER

    claims: list[ClaimCheck] = []
    for item in data.get("afirmaciones") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("texto")
        if not isinstance(text, str) or not text.strip():
            continue
        ids = [str(i).strip().strip("[]").upper() for i in item.get("fragmentos") or [] if isinstance(i, str)]
        ids = [i for i in ids if i in known_ids]
        try:
            verdict = Verdict(item.get("veredicto"))
        except ValueError:
            verdict = Verdict.UNSUPPORTED
        raw_reason = item.get("motivo")
        reason = raw_reason if isinstance(raw_reason, str) else ""
        # "Supported" by no fragment at all is a contradiction in terms: only a
        # cited, known fragment can support a claim.
        if verdict is Verdict.SUPPORTED and not ids:
            verdict = Verdict.UNSUPPORTED
            reason = reason or "La afirmación no cita ningún fragmento válido."
        claims.append(ClaimCheck(text=text.strip(), fragment_ids=ids, verdict=verdict, reason=reason.strip()))
    return VerificationReport(
        response_type=response_type,
        claims=claims,
        declares_no_settled_criterion=data.get("declara_falta_de_criterio") is True,
    )


class Verifier:
    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def verify(self, *, question: str, answer: str, fragments: list[Fragment]) -> VerificationReport:
        try:
            data = self._provider.complete_json(
                system=VERIFIER_SYSTEM_PROMPT,
                user=_build_user_message(question, answer, fragments),
                schema=VERIFIER_SCHEMA,
                schema_name="informe_verificacion",
            )
        except ProviderError as exc:
            logger.warning("verification failed: %s", exc)
            return VerificationReport(error=str(exc))
        return _parse_report(data, {fragment.fragment_id for fragment in fragments})
