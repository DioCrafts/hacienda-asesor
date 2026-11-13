"""The tool-using tax agent: search → answer with citations → verify → revise.

One run answers one user message. The model decides which sources to look up
and how many times; this module owns everything that must not be left to it:
the step budget, the evidence ledger, the independent verification of every
normative claim, and the final verdict shown to the user.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import Enum
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt import settings
from hacienda_gpt.agent.especialistas import Coordinador
from hacienda_gpt.agent.evidence import EvidenceLedger, Fragment, extract_cited_ids
from hacienda_gpt.agent.prompts import (
    AGENT_SYSTEM_PROMPT,
    CASE_TOOLS_ADDENDUM,
    CRITIC_REVISION_MESSAGE,
    FORCE_ANSWER_MESSAGE,
)
from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError, ToolResult
from hacienda_gpt.agent.search import CorpusSearcher
from hacienda_gpt.agent.team import (
    RESEARCH_TOOLS,
    LockedSearcher,
    Plan,
    SubagentRun,
    TeamReport,
    criticar,
    detectar_tributos,
    investigar,
    new_toolbox_factory,
    planificar,
    render_briefing,
    team_mode,
)
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.agent.verifier import ClaimCheck, ResponseType, Verdict, VerificationReport, Verifier
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.doctrine.grafo import DoctrineGraph
from hacienda_gpt.modulos.acciones import AccionPendiente
from hacienda_gpt.modulos.contrato import Modulo

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
    # Validity according to the doctrine graph: vigente | matizada | superada | discutida.
    vigencia: str | None = None
    vigencia_resumen: str | None = None


class ToolTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    tool: str
    arguments: dict[str, Any]
    summary: str
    is_error: bool = False
    # Who made the call: the advisor, or a member of the team.
    agent: str = "asesor"


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    mode: AgentMode
    citations: list[AgentCitation] = Field(default_factory=list)
    claims: list[ClaimCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Cited sources whose validity is in question (superseded, disputed, qualified).
    conflicts: list[str] = Field(default_factory=list)
    # True when the answer itself tells the user the question has no settled criterion.
    sin_criterio_firme: bool = False
    # Set when the question was handled by the team (planner, researchers, critic).
    team: TeamReport | None = None
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
    # Calls to module tools that act outside the app: never executed, the caller stores them as pending.
    acciones_propuestas: list[AccionPendiente] = Field(default_factory=list, exclude=True)


def _context_block(case: CaseState | None) -> str:
    lines = [f"Fecha de hoy: {datetime.now(UTC).date().isoformat()}"]
    if case is not None:
        lines.append(f"Ejercicio del expediente: {case.tax_period} (jurisdicción {case.jurisdiction})")
        if case.facts:
            lines.append("Hechos conocidos: " + "; ".join(f"{fact.name}={fact.value}" for fact in case.facts))
    return "<contexto>\n" + "\n".join(lines) + "\n</contexto>"


def _doctrine_issues(fragments: list[Any], report: VerificationReport) -> tuple[list[str], list[str]]:
    """(flagged sources, warnings) for the doctrine the answer relies on.

    Citing overruled doctrine is fine next to the ruling that overruled it, and
    a disputed criterion is fine when presented as disputed. What is not fine is
    relying on either as if it were settled law.
    """
    conflicts = [
        f"[{f.fragment_id}] {f.title}: {f.vigencia_resumen}"
        for f in fragments
        if f.vigencia in ("superada", "discutida", "matizada")
    ]
    superseded = [f.fragment_id for f in fragments if f.vigencia == "superada"]
    disputed = [f.fragment_id for f in fragments if f.vigencia == "discutida"]
    other_doctrine_cited = any(f.doc_key and f.vigencia not in ("superada", "discutida") for f in fragments)
    warnings: list[str] = []
    if superseded and not (other_doctrine_cited or report.declares_no_settled_criterion):
        warnings.append(
            f"La respuesta se apoya en doctrina que consta como superada ({', '.join(superseded)}) sin citar la "
            "resolución posterior. Contrástala antes de actuar."
        )
    if disputed and not report.declares_no_settled_criterion:
        warnings.append(
            f"Las fuentes citadas recogen un criterio discutido ({', '.join(disputed)}) y la respuesta no advierte "
            "de que no hay criterio firme."
        )
    return conflicts, warnings


def _revision_message(problems: list[ClaimCheck], invalid_ids: list[str], doctrine: list[str] | None = None) -> str:
    lines = [
        "Un verificador independiente ha comparado tu respuesta con los fragmentos citados y ha encontrado "
        "problemas. No se la he mostrado al usuario todavía."
    ]
    if invalid_ids:
        lines.append(f"Identificadores citados que no existen: {', '.join(invalid_ids)}.")
    for issue in doctrine or []:
        lines.append(
            f"- Vigencia: {issue} Busca la resolución posterior y responde con ella, o di expresamente que no hay "
            "criterio firme y expón las posturas."
        )
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
        graph: DoctrineGraph | None = None,
    ) -> None:
        self._graph = graph
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
        on_event: Callable[[dict[str, Any]], None] | None = None,
        ledger: EvidenceLedger | None = None,
        briefing: str | None = None,
        critic: Callable[[str, list[Fragment], Toolbox], str | None] | None = None,
        modulos: Sequence[Modulo] | None = None,
    ) -> AgentResult:
        """Answer ``query``. Raises :class:`ProviderError` if the agent model fails.

        ``modulos`` are the household modules switched on for this user: every one but the tax module adds
        a ``consultar_<clave>`` specialist tool (see :mod:`hacienda_gpt.agent.especialistas`). Without them
        (or with only the tax module) the advisor runs exactly as it always has.

        Team mode passes three extras: a ``ledger`` already holding the
        researchers' fragments, a ``briefing`` with their memos, and a
        ``critic`` called once on the first draft — its objections, if any,
        go back to the model before the answer is verified.

        ``on_event`` receives progress events while the run is in flight
        (``thinking`` / ``text_delta`` / ``tool_call`` / ``tool_result`` /
        ``verifying`` / ``revising``), so a UI can show what the agent is doing instead of a
        silent multi-minute wait. A failing callback never breaks the run.
        """

        def emit(event_type: str, **data: Any) -> None:
            if on_event is None:
                return
            try:
                on_event({"type": event_type, **data})
            except Exception:
                logger.exception("agent progress callback failed")

        ledger = ledger if ledger is not None else EvidenceLedger()
        especialistas = Coordinador.para(self._provider, ledger, modulos, emit)
        toolbox = Toolbox(
            searcher=self._searcher,
            ledger=ledger,
            case=case,
            default_k=self._default_k,
            graph=self._graph,
            extra=especialistas.herramientas() if especialistas is not None else (),
        )
        system = AGENT_SYSTEM_PROMPT + (CASE_TOOLS_ADDENDUM if case is not None else "")
        if especialistas is not None:
            especialistas.vincular(toolbox)
            system += especialistas.addendum()
        conversation = self._provider.start(
            system=system,
            tools=toolbox.specs(),
            history=chat_history or [],  # type: ignore[arg-type]  # normalized by the provider
            user_message="\n\n".join(part for part in (_context_block(case), briefing, query) if part),
        )

        trace: list[ToolTraceEntry] = []
        usage: dict[str, int] = {}
        steps = 0
        revisions = 0
        budget = self._max_steps
        critic_pending = critic is not None

        def finish(**fields: Any) -> AgentResult:
            total = dict(usage)
            full_trace = list(trace)
            if especialistas is not None:
                # The specialists' own calls, for the audit trail; their model use counts towards the answer.
                full_trace += [ToolTraceEntry(**entry) for run in especialistas.runs for entry in run.trace]
                for run in especialistas.runs:
                    for key, value in run.usage.items():
                        total[key] = total.get(key, 0) + value
            return AgentResult(
                tool_trace=full_trace,
                steps=steps,
                revisions=revisions,
                provider=self._provider.name,
                model=self._provider.model,
                usage=total,
                updated_case=toolbox.case if toolbox.case_changed else None,
                acciones_propuestas=list(especialistas.acciones) if especialistas is not None else [],
                **fields,
            )

        while True:
            # On the last allowed step the model must answer: tools are switched off.
            allow_tools = steps < budget - 1
            emit("thinking", step=steps + 1)
            turn = conversation.step(
                allow_tools=allow_tools,
                # Stream the draft only when someone is listening; otherwise the
                # cheaper non-streamed call is used.
                on_text=(lambda chunk: emit("text_delta", text=chunk)) if on_event is not None else None,
            )
            steps += 1
            for key, value in turn.usage.items():
                usage[key] = usage.get(key, 0) + value

            if turn.stop_reason == "refusal":
                return finish(
                    answer=REFUSAL_MESSAGE, mode=AgentMode.ABSTAINED, warnings=["Petición declinada por el modelo."]
                )

            if turn.tool_calls and turn.stop_reason == "max_tokens":
                # The turn was cut off mid tool call: its arguments may be a
                # truncated-but-parseable object, so the tools must not run.
                return finish(
                    answer=ABSTAIN_MESSAGE,
                    mode=AgentMode.ABSTAINED,
                    warnings=["La respuesta del modelo se truncó mientras preparaba una búsqueda."],
                )

            if turn.tool_calls:
                results: list[ToolResult] = []
                for call in turn.tool_calls:
                    if allow_tools:
                        emit("tool_call", tool=call.name, arguments=call.arguments)
                        outcome = toolbox.execute(call)
                        content, summary, is_error = outcome.content, outcome.summary, outcome.is_error
                    else:
                        content = summary = "Sin pasos de búsqueda disponibles; responde con lo que ya tienes."
                        is_error = True
                    emit("tool_result", tool=call.name, summary=summary, is_error=is_error)
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

            if critic_pending and critic is not None:
                # One adversarial pass on the first draft, before spending a
                # verification on text that may be about to change.
                critic_pending = False
                objections = critic(answer, fragments, toolbox)
                if objections:
                    emit("revising", problems=1)
                    budget = steps + _STEPS_PER_REVISION
                    conversation.add_user_message(CRITIC_REVISION_MESSAGE.format(objeciones=objections))
                    continue

            emit("verifying", citations=len(fragments))
            report = self._verifier.verify(question=query, answer=answer, fragments=fragments)

            doctrine_conflicts, doctrine_warnings = _doctrine_issues(fragments, report)
            needs_revision = (
                report.error is None
                and report.response_type is ResponseType.ANSWER
                and bool(report.problems or invalid_ids or doctrine_warnings)
                and revisions < self._max_revisions
            )
            if needs_revision:
                revisions += 1
                emit("revising", problems=len(report.problems) + len(invalid_ids) + len(doctrine_warnings))
                budget = steps + _STEPS_PER_REVISION
                conversation.add_user_message(
                    _revision_message(report.problems, invalid_ids, doctrine_conflicts if doctrine_warnings else None)
                )
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
                vigencia=f.vigencia,
                vigencia_resumen=f.vigencia_resumen,
            )
            for f in fragments
        ]
        warnings: list[str] = []
        if invalid_ids:
            warnings.append(f"La respuesta cita fuentes inexistentes: {', '.join(invalid_ids)}.")
        conflicts, doctrine_warnings = _doctrine_issues(fragments, report)
        warnings.extend(doctrine_warnings)
        unflagged = bool(doctrine_warnings)
        base: dict[str, Any] = {
            "answer": answer,
            "citations": citations,
            "claims": report.claims,
            "conflicts": conflicts,
            "sin_criterio_firme": report.declares_no_settled_criterion,
        }

        if report.error is not None:
            warnings.append(f"No se pudo verificar la respuesta contra sus fuentes: {report.error}")
            return {**base, "mode": AgentMode.UNVERIFIED, "warnings": warnings}
        if report.response_type is ResponseType.CLARIFICATION:
            return {**base, "mode": AgentMode.CLARIFICATION, "warnings": warnings}
        if report.response_type is ResponseType.ABSTENTION:
            return {**base, "mode": AgentMode.ABSTAINED, "warnings": warnings}

        for claim in report.problems:
            lead = (
                "Solo respaldada en parte por la fuente citada"
                if claim.verdict is Verdict.PARTIAL
                else "Sin respaldo en la fuente citada"
            )
            warnings.append(f"{lead}: «{claim.text}». {claim.reason}".strip())

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
        if report.problems or invalid_ids or unflagged:
            return {**base, "mode": AgentMode.PARTIALLY_VERIFIED, "warnings": warnings}
        return {**base, "mode": AgentMode.VERIFIED, "warnings": warnings}


class TeamAgent(TaxAgent):
    """The advisor, with a team it calls in only when a question needs one.

    ``mode`` (default: ``settings.AGENT_TEAM_MODE``):

    * ``auto`` — the single agent answers. The team takes over when (a) the
      question mentions two or more taxes **and** the planner confirms it is
      complex, or (b) the single agent's answer relies on superseded or
      disputed sources that it did not resolve.
    * ``always`` / ``never`` — force one path.
    """

    def __init__(
        self, *args: Any, mode: str | None = None, subagent_max_steps: int | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._mode = mode if mode in ("auto", "always", "never") else team_mode()
        self._subagent_max_steps = subagent_max_steps if subagent_max_steps is not None else settings.SUBAGENT_MAX_STEPS

    def run(
        self,
        query: str,
        *,
        chat_history: list[dict[str, Any]] | None = None,
        case: CaseState | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        modulos: Sequence[Modulo] | None = None,
        **kwargs: Any,
    ) -> AgentResult:
        if self._mode == "never" or kwargs:
            return super().run(
                query, chat_history=chat_history, case=case, on_event=on_event, modulos=modulos, **kwargs
            )

        def emit(event_type: str, **data: Any) -> None:
            if on_event is None:
                return
            try:
                on_event({"type": event_type, **data})
            except Exception:
                logger.exception("agent progress callback failed")

        tributos = detectar_tributos(query)
        plan: Plan | None = None
        if self._mode == "always" or len(tributos) >= 2:
            emit("team", stage="planning", agent="planificador", status="start")
            plan = planificar(self._provider, query, case)
            emit("team", stage="planning", agent="planificador", status="done", complex=bool(plan and plan.compleja))

        if self._mode == "always" or (plan is not None and plan.compleja):
            plan = plan or Plan(compleja=True, motivo="Modo equipo forzado por configuración.", tributos=tributos)
            trigger = "planificador" if plan.compleja and self._mode != "always" else "forzado"
            return self._run_team(
                query,
                plan,
                trigger,
                chat_history=chat_history,
                case=case,
                on_event=on_event,
                emit=emit,
                modulos=modulos,
            )

        result = super().run(query, chat_history=chat_history, case=case, on_event=on_event, modulos=modulos)
        # Escalate when the single agent ran into doctrine it could not settle.
        if result.conflicts and result.mode is not AgentMode.VERIFIED:
            emit("team", stage="escalation", agent="planificador", status="start")
            plan = Plan(
                compleja=True,
                motivo="La primera respuesta se apoyaba en fuentes superadas o discutidas sin resolverlo.",
                tributos=tributos,
                encargo_doctrina=(
                    "La primera investigación encontró estas fuentes con la vigencia en cuestión: "
                    + " | ".join(result.conflicts)
                    + ". Localiza las resoluciones posteriores, determina cuál es el criterio vigente y, si las "
                    "fuentes de rango comparable siguen discrepando, dilo."
                ),
            )
            team_result = self._run_team(
                query,
                plan,
                "conflicto",
                chat_history=chat_history,
                # Facts the first pass recorded must not be lost.
                case=result.updated_case or case,
                on_event=on_event,
                emit=emit,
                modulos=modulos,
            )
            for key, value in result.usage.items():
                team_result.usage[key] = team_result.usage.get(key, 0) + value
            if team_result.updated_case is None:
                team_result.updated_case = result.updated_case
            # Actions the first pass left pending are still the user's to decide.
            team_result.acciones_propuestas = [*result.acciones_propuestas, *team_result.acciones_propuestas]
            return team_result
        return result

    def _run_team(
        self,
        query: str,
        plan: Plan,
        trigger: str,
        *,
        chat_history: list[dict[str, Any]] | None,
        case: CaseState | None,
        on_event: Callable[[dict[str, Any]], None] | None,
        emit: Callable[..., None],
        modulos: Sequence[Modulo] | None = None,
    ) -> AgentResult:
        ledger = EvidenceLedger()
        make_toolbox = new_toolbox_factory(
            searcher=LockedSearcher(self._searcher),
            ledger=ledger,
            case=case,
            default_k=self._default_k,
            graph=self._graph,
        )
        runs = investigar(
            self._provider,
            plan=plan,
            query=query,
            case=case,
            make_toolbox=make_toolbox,
            emit=emit,
            max_steps=self._subagent_max_steps,
            parallel=settings.SUBAGENTS_PARALLEL,
        )
        critic_runs: list[SubagentRun] = []

        def critic(draft: str, fragments: list[Fragment], _toolbox: Toolbox) -> str | None:
            run = criticar(
                self._provider,
                query=query,
                case=case,
                draft=draft,
                fragments=fragments,
                toolbox=make_toolbox(RESEARCH_TOOLS),
                emit=emit,
                max_steps=self._subagent_max_steps,
            )
            critic_runs.append(run)
            return run.memo or None

        result = super().run(
            query,
            chat_history=chat_history,
            case=case,
            on_event=on_event,
            ledger=ledger,
            briefing=render_briefing(runs),
            critic=critic,
            modulos=modulos,
        )
        all_runs = [*runs, *critic_runs]
        result.tool_trace = [
            *(ToolTraceEntry(**entry) for run in all_runs for entry in run.trace),
            *result.tool_trace,
        ]
        for run in all_runs:
            for key, value in run.usage.items():
                result.usage[key] = result.usage.get(key, 0) + value
        result.team = TeamReport(
            trigger=trigger,
            motivo=plan.motivo,
            tributos=plan.tributos,
            subagents=[run.summary() for run in all_runs],
            objections=next((run.memo for run in critic_runs if run.memo), None),
        )
        return result


def build_agent() -> TaxAgent:
    """Wire the production agent from settings (provider, FAISS searcher, verifier)."""
    from hacienda_gpt.agent.providers import create_provider
    from hacienda_gpt.agent.search import FaissCorpusSearcher
    from hacienda_gpt.ops.consumo import medir

    # Every call is recorded and checked against the configured budgets.
    provider = medir(create_provider(), "agente")
    verifier_provider = provider.con_proposito("verificador")
    if settings.VERIFIER_PROVIDER or settings.VERIFIER_MODEL:
        verifier_provider = medir(
            create_provider(settings.VERIFIER_PROVIDER or settings.LLM_PROVIDER, model=settings.VERIFIER_MODEL or None),
            "verificador",
        )
    graph = None
    if Path(settings.DOCTRINE_GRAPH_PATH).exists():
        graph = DoctrineGraph(settings.DOCTRINE_GRAPH_PATH)
    else:
        logger.info("No doctrine graph at %s: validity alerts are disabled.", settings.DOCTRINE_GRAPH_PATH)
    return TeamAgent(
        provider=provider,
        searcher=FaissCorpusSearcher.from_settings(),
        verifier=Verifier(verifier_provider),
        graph=graph,
    )


__all__ = ["AgentMode", "AgentResult", "ProviderError", "TaxAgent", "TeamAgent", "build_agent"]
