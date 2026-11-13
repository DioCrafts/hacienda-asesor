"""Team mode: planner → two researchers → advisor → adversarial critic.

A single agent is the right tool for most questions. It is the wrong one when a
question spans several taxes or runs into conflicting doctrine: one context
window tends to resolve the first tax well and the second one thinly, and
nobody argues against the draft. For those cases only, this module splits the
work:

* the **planner** decides whether the question is complex and writes two
  self-contained briefs;
* the **normativa** and **doctrina** researchers investigate in parallel, each
  with its own context and a restricted tool set, writing into the shared
  evidence ledger so their fragment ids are citable by everyone;
* the **advisor** (the ordinary agent loop) drafts the answer from their memos;
* the **critic** attacks the draft the way a tax inspector would, and the
  advisor must answer its objections before the usual claim verification.

Everything is opt-in by need (see :meth:`TeamAgent.run`): a simple question
costs exactly what it cost before.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import dataclass, field
import logging
import re
import threading
from typing import Any

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt import settings
from hacienda_gpt.agent.evidence import EvidenceLedger, Fragment
from hacienda_gpt.agent.prompts import (
    CRITIC_SYSTEM_PROMPT,
    PLANNER_SCHEMA,
    PLANNER_SYSTEM_PROMPT,
    RESEARCHER_DOCTRINA_PROMPT,
    RESEARCHER_NORMATIVA_PROMPT,
    TEAM_BRIEFING_HEADER,
)
from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError, ToolResult
from hacienda_gpt.agent.search import CorpusSearcher, SearchFilters
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.decision.schemas import CaseState

logger = logging.getLogger(__name__)

Emit = Callable[..., None]

RESEARCH_TOOLS = frozenset({"buscar_fuentes", "consultar_vigencia"})
NO_OBJECTIONS = "SIN OBJECIONES"

# Taxes a question may involve, by the words people use for them.
_TRIBUTOS: dict[str, str] = {
    "IRPF": r"\birpf\b|\brenta\b|declaraci[oó]n de la renta|ganancia patrimonial|rendimientos? del trabajo",
    "IVA": r"\biva\b|modelo 303|modelo 390|intracomunitari",
    "Sucesiones y Donaciones": r"\bisd\b|sucesiones|herencia|hered[aoóeé]|donaci[oó]n|\bdonar\b|legado",
    "Patrimonio": r"impuesto (?:sobre el |de(?:l)? )patrimonio|grandes fortunas",
    "Sociedades": r"impuesto (?:sobre|de) sociedades|\bmi (?:sl|sociedad|empresa)\b|\bs\.?l\.?\b",
    "Plusvalía municipal": r"plusval[ií]a municipal|\biivtnu\b",
    "ITP y AJD": r"\bitp\b|transmisiones patrimoniales|actos jur[ií]dicos documentados|\bajd\b",
    "No Residentes": r"no residentes?|\birnr\b|modelo 210|residencia fiscal en (?!espa)",
    "Declaraciones informativas": r"modelo 720|modelo 721|bienes en el extranjero",
}


def detectar_tributos(text: str) -> list[str]:
    """Taxes mentioned in ``text`` (cheap pre-filter; the planner has the final say)."""
    lowered = text.lower()
    return [nombre for nombre, pattern in _TRIBUTOS.items() if re.search(pattern, lowered)]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compleja: bool
    motivo: str = ""
    tributos: list[str] = Field(default_factory=list)
    encargo_normativa: str = ""
    encargo_doctrina: str = ""


class SubagentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    steps: int
    searches: int
    produced_memo: bool


class TeamReport(BaseModel):
    """What the team did, for the audit trail and the UI."""

    model_config = ConfigDict(extra="forbid")

    # "planificador" (complex by plan) | "conflicto" (escalated after the single agent) | "forzado"
    trigger: str
    motivo: str
    tributos: list[str] = Field(default_factory=list)
    subagents: list[SubagentSummary] = Field(default_factory=list)
    # The critic's objections, as given to the advisor (None: none raised or critic unavailable).
    objections: str | None = None


def _facts_line(case: CaseState | None) -> str:
    if case is None or not case.facts:
        return "(sin datos del usuario)"
    return f"Ejercicio {case.tax_period}; " + "; ".join(f"{fact.name}={fact.value}" for fact in case.facts)


def planificar(provider: LLMProvider, query: str, case: CaseState | None) -> Plan | None:
    """Ask the planner. None when it fails: the caller falls back to the single agent."""
    try:
        data = provider.complete_json(
            system=PLANNER_SYSTEM_PROMPT,
            user=f"<consulta>\n{query}\n</consulta>\n\n<datos_del_usuario>\n{_facts_line(case)}\n</datos_del_usuario>",
            schema=PLANNER_SCHEMA,
            schema_name="plan_de_investigacion",
        )
    except ProviderError as exc:
        logger.warning("planner failed, using the single agent: %s", exc)
        return None
    if not isinstance(data.get("compleja"), bool):
        return None
    return Plan(
        compleja=data["compleja"],
        motivo=str(data.get("motivo") or "").strip(),
        tributos=[str(t).strip() for t in data.get("tributos") or [] if str(t).strip()],
        encargo_normativa=str(data.get("encargo_normativa") or "").strip(),
        encargo_doctrina=str(data.get("encargo_doctrina") or "").strip(),
    )


class LockedSearcher:
    """Serialises corpus searches: the embedder and the reranker are not safe to
    call from two threads, while the researchers' LLM calls are."""

    def __init__(self, inner: CorpusSearcher) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def search(self, query: str, *, k: int, filters: SearchFilters) -> list[Document]:
        with self._lock:
            return self._inner.search(query, k=k, filters=filters)


@dataclass
class SubagentRun:
    agent: str
    memo: str = ""
    steps: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def summary(self) -> SubagentSummary:
        return SubagentSummary(
            agent=self.agent,
            steps=self.steps,
            searches=sum(1 for entry in self.trace if entry["tool"] == "buscar_fuentes" and not entry["is_error"]),
            produced_memo=bool(self.memo),
        )


def run_subagent(
    provider: LLMProvider,
    *,
    agent: str,
    system: str,
    toolbox: Toolbox,
    user_message: str,
    max_steps: int,
    emit: Emit,
) -> SubagentRun:
    """A bounded tool loop that ends in a memo (the subagent's last text turn).

    A subagent that fails or produces nothing yields an empty memo: the team
    degrades to fewer inputs rather than failing the user's question.
    """
    run = SubagentRun(agent=agent)
    try:
        conversation = provider.start(system=system, tools=toolbox.specs(), history=[], user_message=user_message)
        while run.steps < max_steps:
            allow_tools = run.steps < max_steps - 1
            turn = conversation.step(allow_tools=allow_tools)
            run.steps += 1
            for key, value in turn.usage.items():
                run.usage[key] = run.usage.get(key, 0) + value
            if turn.stop_reason == "refusal" or (turn.tool_calls and turn.stop_reason == "max_tokens"):
                break
            if not turn.tool_calls:
                run.memo = turn.text.strip()
                break
            results: list[ToolResult] = []
            for call in turn.tool_calls:
                if allow_tools:
                    emit("tool_call", tool=call.name, arguments=call.arguments, agent=agent)
                    outcome = toolbox.execute(call)
                    content, summary, is_error = outcome.content, outcome.summary, outcome.is_error
                else:
                    content = summary = "Sin más pasos: entrega ya tu informe con lo que tienes."
                    is_error = True
                emit("tool_result", tool=call.name, summary=summary, is_error=is_error, agent=agent)
                results.append(ToolResult(call_id=call.id, content=content, is_error=is_error))
                run.trace.append(
                    {
                        "step": run.steps,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "summary": summary,
                        "is_error": is_error,
                        "agent": agent,
                    }
                )
            conversation.add_tool_results(results)
    except ProviderError as exc:
        logger.warning("subagent %s failed: %s", agent, exc)
    return run


def investigar(
    provider: LLMProvider,
    *,
    plan: Plan,
    query: str,
    case: CaseState | None,
    make_toolbox: Callable[[frozenset[str]], Toolbox],
    emit: Emit,
    max_steps: int,
    parallel: bool,
) -> list[SubagentRun]:
    """Run the two researchers and return their runs (normativa first)."""
    datos = _facts_line(case)

    def brief(encargo: str, fallback: str) -> str:
        return (
            f"<encargo>\n{encargo or fallback}\n</encargo>\n\n<consulta_original>\n{query}\n</consulta_original>\n\n"
            f"<datos_del_usuario>\n{datos}\n</datos_del_usuario>"
        )

    jobs = [
        (
            "investigador_normativa",
            RESEARCHER_NORMATIVA_PROMPT,
            brief(plan.encargo_normativa, "Localiza los preceptos de ley y reglamento aplicables a la consulta."),
        ),
        (
            "investigador_doctrina",
            RESEARCHER_DOCTRINA_PROMPT,
            brief(
                plan.encargo_doctrina,
                "Localiza los criterios de la DGT, del TEAC y de los tribunales sobre la consulta y comprueba su vigencia.",
            ),
        ),
    ]

    def one(job: tuple[str, str, str]) -> SubagentRun:
        agent, system, message = job
        emit("team", stage="research", agent=agent, status="start")
        run = run_subagent(
            provider,
            agent=agent,
            system=system,
            toolbox=make_toolbox(RESEARCH_TOOLS),
            user_message=message,
            max_steps=max_steps,
            emit=emit,
        )
        emit("team", stage="research", agent=agent, status="done")
        return run

    if parallel:
        # Each worker thread gets a copy of the caller's context, so usage metering
        # still knows whose question this is.
        contexts = [contextvars.copy_context() for _ in jobs]
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="researcher") as pool:
            return list(pool.map(lambda pair: pair[0].run(one, pair[1]), zip(contexts, jobs, strict=True)))
    return [one(job) for job in jobs]


def render_briefing(runs: list[SubagentRun]) -> str | None:
    titles = {
        "investigador_normativa": "Informe del investigador de normativa",
        "investigador_doctrina": "Informe del investigador de doctrina y jurisprudencia",
    }
    memos = [f"### {titles.get(run.agent, run.agent)}\n{run.memo}" for run in runs if run.memo]
    if not memos:
        return None
    return f"<investigacion_previa>\n{TEAM_BRIEFING_HEADER}\n\n" + "\n\n".join(memos) + "\n</investigacion_previa>"


def criticar(
    provider: LLMProvider,
    *,
    query: str,
    case: CaseState | None,
    draft: str,
    fragments: list[Fragment],
    toolbox: Toolbox,
    emit: Emit,
    max_steps: int,
) -> SubagentRun:
    """Adversarial review of a draft. ``run.memo`` holds the objections ("" = none)."""
    cited = "\n\n".join(fragment.render() for fragment in fragments) or "(el borrador no cita ningún fragmento)"
    emit("team", stage="critic", agent="critico", status="start")
    run = run_subagent(
        provider,
        agent="critico",
        system=CRITIC_SYSTEM_PROMPT,
        toolbox=toolbox,
        user_message=(
            f"<consulta>\n{query}\n</consulta>\n\n<datos_del_usuario>\n{_facts_line(case)}\n</datos_del_usuario>\n\n"
            f"<borrador>\n{draft}\n</borrador>\n\n<fragmentos_citados>\n{cited}\n</fragmentos_citados>"
        ),
        max_steps=max_steps,
        emit=emit,
    )
    if NO_OBJECTIONS in run.memo.upper() and len(run.memo) < 80:
        run.memo = ""
    emit("team", stage="critic", agent="critico", status="done", objections=bool(run.memo))
    return run


def new_toolbox_factory(
    *, searcher: CorpusSearcher, ledger: EvidenceLedger, case: CaseState | None, default_k: int, graph: Any
) -> Callable[[frozenset[str]], Toolbox]:
    """Toolboxes for subagents: shared ledger and graph, restricted tool set."""

    def make(allowed: frozenset[str]) -> Toolbox:
        return Toolbox(searcher=searcher, ledger=ledger, case=case, default_k=default_k, graph=graph, allowed=allowed)

    return make


def team_mode() -> str:
    mode = settings.AGENT_TEAM_MODE.strip().lower()
    return mode if mode in ("auto", "always", "never") else "auto"
