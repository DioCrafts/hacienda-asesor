"""The tool-using tax agent: search → answer with citations → verify → revise.

One run answers one user message. The model decides which sources to look up
and how many times; this module owns everything that must not be left to it:
the step budget, the evidence ledger, the independent verification of every
normative claim, and the final verdict shown to the user.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt import settings
from hacienda_gpt.agent.evidence import EvidenceLedger, extract_cited_ids
from hacienda_gpt.agent.prompts import AGENT_SYSTEM_PROMPT, CASE_TOOLS_ADDENDUM, FORCE_ANSWER_MESSAGE
from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError, ToolResult
from hacienda_gpt.agent.search import CorpusSearcher
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.agent.verifier import ClaimCheck, ResponseType, VerificationReport, Verifier
from hacienda_gpt.decision.schemas import CaseState

logger = logging.getLogger(__name__)

ABSTAIN_MESSAGE = (
    "No he encontrado en mis fuentes base normativa suficiente para responder con seguridad a esta consulta. "
    "Te recomiendo contrastarla con la Agencia Tributaria o con un asesor fiscal colegiado."
)
REFUSAL_MESSAGE = "El modelo ha declinado responder a esta consulta."
# Extra model turns granted to act on a verifier rejection (search + rewrite).
_STEPS_PER_REVISION = 3


class AgentMode(str, Enum):
    VERIFIED = "verified"  # every normative claim is backed by its cited fragment
    PARTIALLY_VERIFIED = "partially_verified"  # some claims are flagged in `warnings`
    UNVERIFIED = "unverified"  # nothing to check against, or the check could not run
    CLARIFICATION = "clarification"  # the agent needs a fact from the user first
    ABSTAINED = "abstained"  # no sufficient basis; canned message


class AgentCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    title: str
    locator: str
    section: str | None = None
    authority: str | None = None
    source_type: str | None = None
    binding: bool | None = None
    date: str | None = None
    reference: str | None = None
    snippet: str | None = None


class ToolTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    tool: str
    arguments: dict[str, Any]
    summary: str
    is_error: bool = False


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    mode: AgentMode
    citations: list[AgentCitation] = Field(default_factory=list)
    claims: list[ClaimCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # The model's original text when `answer` was replaced by the abstain message.
    raw_answer: str | None = None
    tool_trace: list[ToolTraceEntry] = Field(default_factory=list)
    steps: int = 0
    revisions: int = 0
    provider: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)
    # Present only when a case was attached AND a tool changed it; the caller persists it.
    updated_case: CaseState | None = Field(default=None, exclude=True)


def _context_block(case: CaseState | None) -> str:
    lines = [f"Fecha de hoy: {datetime.now(UTC).date().isoformat()}"]
    if case is not None:
        lines.append(f"Ejercicio del expediente: {case.tax_period} (jurisdicción {case.jurisdiction})")
        if case.facts:
            lines.append("Hechos conocidos: " + "; ".join(f"{fact.name}={fact.value}" for fact in case.facts))
    return "<contexto>\n" + "\n".join(lines) + "\n</contexto>"


def _revision_message(problems: list[ClaimCheck], invalid_ids: list[str]) -> str:
    lines = [
        "Un verificador independiente ha comparado tu respuesta con los fragmentos citados y ha encontrado "
        "problemas. No se la he mostrado al usuario todavía."
    ]
    if invalid_ids:
        lines.append(f"Identificadores citados que no existen: {', '.join(invalid_ids)}.")
    for claim in problems:
        cited = ", ".join(claim.fragment_ids) or "sin cita"
        lines.append(f"- «{claim.text}» ({cited}) → {claim.verdict.value}: {claim.reason or 'sin respaldo'}")
    lines.append(
        "Reescribe la respuesta completa para el usuario. Si crees que alguna de estas afirmaciones tiene respaldo, "
        "búscalo y cítalo; si no lo encuentras, corrígela para que diga solo lo que dice la fuente o elimínala, y "
        "avisa al usuario de lo que no has podido confirmar. Devuelve únicamente la respuesta final."
    )
    return "\n".join(lines)


class TaxAgent:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        searcher: CorpusSearcher,
        verifier: Verifier | None = None,
        max_steps: int | None = None,
        max_revisions: int | None = None,
        default_k: int | None = None,
    ) -> None:
        self._provider = provider
        self._searcher = searcher
        self._verifier = verifier or Verifier(provider)
        self._max_steps = max_steps if max_steps is not None else settings.AGENT_MAX_STEPS
        self._max_revisions = max_revisions if max_revisions is not None else settings.AGENT_MAX_REVISIONS
        self._default_k = default_k if default_k is not None else settings.AGENT_SEARCH_DEFAULT_K

    def run(
        self,
        query: str,
        *,
        chat_history: list[dict[str, Any]] | None = None,
        case: CaseState | None = None,
    ) -> AgentResult:
        """Answer ``query``. Raises :class:`ProviderError` if the agent model fails."""
        ledger = EvidenceLedger()
        toolbox = Toolbox(searcher=self._searcher, ledger=ledger, case=case, default_k=self._default_k)
        system = AGENT_SYSTEM_PROMPT + (CASE_TOOLS_ADDENDUM if case is not None else "")
        conversation = self._provider.start(
            system=system,
            tools=toolbox.specs(),
            history=chat_history or [],  # type: ignore[arg-type]  # normalized by the provider
            user_message=f"{_context_block(case)}\n\n{query}",
        )

        trace: list[ToolTraceEntry] = []
        usage: dict[str, int] = {}
        steps = 0
        revisions = 0
        budget = self._max_steps

        def finish(**fields: Any) -> AgentResult:
            return AgentResult(
                tool_trace=trace,
                steps=steps,
                revisions=revisions,
                provider=self._provider.name,
                model=self._provider.model,
                usage=usage,
                updated_case=toolbox.case if toolbox.case_changed else None,
                **fields,
            )

        while True:
            # On the last allowed step the model must answer: tools are switched off.
            allow_tools = steps < budget - 1
            turn = conversation.step(allow_tools=allow_tools)
            steps += 1
            for key, value in turn.usage.items():
                usage[key] = usage.get(key, 0) + value

            if turn.stop_reason == "refusal":
                return finish(
                    answer=REFUSAL_MESSAGE, mode=AgentMode.ABSTAINED, warnings=["Petición declinada por el modelo."]
                )

            if turn.tool_calls:
                results: list[ToolResult] = []
                for call in turn.tool_calls:
                    if allow_tools:
                        outcome = toolbox.execute(call)
                        content, summary, is_error = outcome.content, outcome.summary, outcome.is_error
                    else:
                        content = summary = "Sin pasos de búsqueda disponibles; responde con lo que ya tienes."
                        is_error = True
                    results.append(ToolResult(call_id=call.id, content=content, is_error=is_error))
                    trace.append(
                        ToolTraceEntry(
                            step=steps, tool=call.name, arguments=call.arguments, summary=summary, is_error=is_error
                        )
                    )
                conversation.add_tool_results(results)
                if steps >= budget:
                    # The model ignored tool_choice=none (some local servers do).
                    return finish(
                        answer=ABSTAIN_MESSAGE,
                        mode=AgentMode.ABSTAINED,
                        warnings=["El agente agotó sus pasos sin llegar a una respuesta."],
                    )
                if steps == budget - 1:
                    conversation.add_user_message(FORCE_ANSWER_MESSAGE)
                continue

            answer = turn.text
            if not answer:
                reason = (
                    "La respuesta se truncó por límite de tokens."
                    if turn.stop_reason == "max_tokens"
                    else "El modelo no devolvió texto."
                )
                return finish(answer=ABSTAIN_MESSAGE, mode=AgentMode.ABSTAINED, warnings=[reason])

            cited_ids = extract_cited_ids(answer)
            invalid_ids = [fragment_id for fragment_id in cited_ids if ledger.get(fragment_id) is None]
            fragments = [f for f in (ledger.get(fragment_id) for fragment_id in cited_ids) if f is not None]
            report = self._verifier.verify(question=query, answer=answer, fragments=fragments)

            needs_revision = (
                report.error is None
                and report.response_type is ResponseType.ANSWER
                and bool(report.problems or invalid_ids)
                and revisions < self._max_revisions
            )
            if needs_revision:
                revisions += 1
                budget = steps + _STEPS_PER_REVISION
                conversation.add_user_message(_revision_message(report.problems, invalid_ids))
                continue

            return finish(**self._verdict(answer, report, fragments, invalid_ids))

    @staticmethod
    def _verdict(
        answer: str,
        report: VerificationReport,
        fragments: list[Any],
        invalid_ids: list[str],
    ) -> dict[str, Any]:
        citations = [
            AgentCitation(
                fragment_id=f.fragment_id,
                title=f.title,
                locator=f.locator,
                section=f.section,
                authority=f.authority,
                source_type=f.source_type,
                binding=f.binding,
                date=f.date,
                reference=f.reference,
                snippet=f.text[:300] + ("…" if len(f.text) > 300 else ""),
            )
            for f in fragments
        ]
        warnings: list[str] = []
        if invalid_ids:
            warnings.append(f"La respuesta cita fuentes inexistentes: {', '.join(invalid_ids)}.")
        base: dict[str, Any] = {"answer": answer, "citations": citations, "claims": report.claims}

        if report.error is not None:
            warnings.append(f"No se pudo verificar la respuesta contra sus fuentes: {report.error}")
            return {**base, "mode": AgentMode.UNVERIFIED, "warnings": warnings}
        if report.response_type is ResponseType.CLARIFICATION:
            return {**base, "mode": AgentMode.CLARIFICATION, "warnings": warnings}
        if report.response_type is ResponseType.ABSTENTION:
            return {**base, "mode": AgentMode.ABSTAINED, "warnings": warnings}

        for claim in report.problems:
            warnings.append(f"Sin respaldo suficiente ({claim.verdict.value}): «{claim.text}». {claim.reason}".strip())

        if not report.claims:
            warnings.append("La respuesta no contiene afirmaciones normativas verificables; trátala como orientativa.")
            return {**base, "mode": AgentMode.UNVERIFIED, "warnings": warnings}
        if not report.supported:
            # Nothing in the answer survived the check: showing it would present
            # unsupported tax advice as if it were sourced.
            return {
                **base,
                "answer": ABSTAIN_MESSAGE,
                "raw_answer": answer,
                "mode": AgentMode.ABSTAINED,
                "warnings": warnings,
            }
        if report.problems or invalid_ids:
            return {**base, "mode": AgentMode.PARTIALLY_VERIFIED, "warnings": warnings}
        return {**base, "mode": AgentMode.VERIFIED, "warnings": warnings}


def build_agent() -> TaxAgent:
    """Wire the production agent from settings (provider, FAISS searcher, verifier)."""
    from hacienda_gpt.agent.providers import create_provider
    from hacienda_gpt.agent.search import FaissCorpusSearcher

    provider = create_provider()
    verifier_provider = provider
    if settings.VERIFIER_PROVIDER or settings.VERIFIER_MODEL:
        verifier_provider = create_provider(
            settings.VERIFIER_PROVIDER or settings.LLM_PROVIDER, model=settings.VERIFIER_MODEL or None
        )
    return TaxAgent(
        provider=provider,
        searcher=FaissCorpusSearcher.from_settings(),
        verifier=Verifier(verifier_provider),
    )


__all__ = ["AgentMode", "AgentResult", "ProviderError", "TaxAgent", "build_agent"]
