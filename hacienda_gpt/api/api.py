from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
import json
import queue
import threading
from typing import Annotated, TypeVar
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def _thread_safe_singleton(factory: Callable[[], T]) -> Callable[[], T]:
    """Lazy, thread-safe replacement for ``functools.lru_cache(maxsize=1)``.

    ``functools.lru_cache`` is documented as **not** thread-safe for
    concurrent first calls: under a request burst (e.g. uvicorn worker
    thread pool serving the first ``/qa``) two threads can both observe a
    cache miss and run ``factory()`` in parallel. For ``_build_qa_chain``
    that meant loading the 1.1 GB MLX embedder twice into RAM. This
    decorator wraps the factory in double-checked locking so the work
    happens exactly once.

    A FAILED build is cached too: a misconfiguration (missing OPENAI_API_KEY,
    absent FAISS index, a model that won't load) would otherwise re-run the
    expensive factory — reloading the multi-GB embedder — on *every* request.
    The cached exception is re-raised cheaply instead. ``cache_clear()`` resets
    both success and failure so a fixed deployment can recover in-process (and
    so tests start clean).
    """
    lock = threading.Lock()
    holder: list[T] = []
    error_holder: list[Exception] = []

    def wrapper() -> T:
        if holder:
            return holder[0]
        if error_holder:
            raise error_holder[0]
        with lock:
            if holder:
                return holder[0]
            if error_holder:
                raise error_holder[0]
            try:
                holder.append(factory())
            except Exception as exc:
                error_holder.append(exc)
                raise
            return holder[0]

    def cache_clear() -> None:
        with lock:
            holder.clear()
            error_holder.clear()

    wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
    return wrapper


from hacienda_gpt.agent.agent import AgentResult, TaxAgent
from hacienda_gpt.agent.providers.base import LLMProvider, ProviderError
from hacienda_gpt.api.observability import ApiMetrics, observability_middleware
from hacienda_gpt.api.security import (
    RateLimiter,
    make_rate_limit_middleware,
    require_api_key,
)
from hacienda_gpt.decision.audit import build_recommendation_audit_event
from hacienda_gpt.decision.fact_extractor import (
    EXPECTED_FACT_TYPE,
    FactExtractor,
    OpenAIExtractionError,
    _coerce_value,
    default_fact_extractor,
)
from hacienda_gpt.decision.planner import Planner
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import (
    CaseState,
    Fact,
    MissingFact,
    ObligationCandidate,
    parse_fiscal_year,
)
from hacienda_gpt.decision.state_store import CaseVersionConflictError
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.decision.turn_service import process_turn
from hacienda_gpt.llm.grounding import AnswerEnvelope
from hacienda_gpt.proactive.jobs import ejecutar_para_caso, hoy_en_espana
from hacienda_gpt.proactive.store import Notification, NotificationStore
from hacienda_gpt.settings import API_RATE_LIMIT_PER_MIN, DECISION_STATE_DB_PATH
from hacienda_gpt.tax.calendario import Vencimiento, aviso_festivos, proximos_vencimientos
from hacienda_gpt.tax.documentos import AnalisisDocumento, DocumentoNoLegibleError, analizar_documento, decode_upload
from hacienda_gpt.tax.params import EjercicioNoDisponibleError, ejercicios_disponibles
from hacienda_gpt.tax.profile import DatosInvalidosError
from hacienda_gpt.tax.simulador import Simulacion, simular

app = FastAPI(title="HaciendaGPT Decision API", version="1.0.0")

# CORS: in dev the Vite frontend runs at http://localhost:5173 and
# proxies /api/* to this server, which avoids preflight altogether. We
# still register CORSMiddleware so a non-proxied React build (e.g. a
# preview deploy on a different host) can hit the API directly. The
# allow-list is deliberately narrow — wildcard origins would be a footgun
# the day someone exposes the API publicly.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",  # vite preview
    "http://127.0.0.1:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    allow_credentials=False,
)

# Observability + rate limiting. Registration order matters: Starlette runs the
# LAST-registered middleware OUTERMOST, so register the limiter first and the
# observability layer second — the latter then times/logs/meters every request
# including the ones the limiter rejects with 429.
app.state.api_metrics = ApiMetrics()
_rate_limiter = RateLimiter(API_RATE_LIMIT_PER_MIN)
app.middleware("http")(make_rate_limit_middleware(_rate_limiter))
app.middleware("http")(observability_middleware)


@_thread_safe_singleton
def _build_case_store() -> SQLiteCaseStateStore:
    # Single configurable store shared with the Streamlit UI; the path was
    # previously hardcoded, so the API and UI never saw each other's cases.
    # Cached so the schema DDL and the SQLite connection are set up once,
    # not rebuilt on every request (mirrors _build_qa_chain).
    return SQLiteCaseStateStore(DECISION_STATE_DB_PATH)


def get_case_store() -> SQLiteCaseStateStore:
    # Thin wrapper so tests can still override it via app.dependency_overrides.
    return _build_case_store()


@_thread_safe_singleton
def _build_fact_extractor() -> FactExtractor:
    # Built once and reused, mirroring _build_qa_chain / _build_case_store. The
    # previous per-request construction spun up a fresh OpenAI() client on every
    # turn; the extractor's backend choice is fixed by the environment at
    # process start, so there is nothing to recompute per request.
    return default_fact_extractor()


def get_fact_extractor() -> FactExtractor:
    # Thin wrapper so tests can still override it via app.dependency_overrides.
    return _build_fact_extractor()


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    jurisdiction: str = Field(default="ES", min_length=2)
    tax_period: str = Field(min_length=4)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_input: str = Field(min_length=1)
    # Prior conversation turns ({"role": "user"|"assistant", "content": str}).
    # Forwarded to the extractor so multi-turn references ("y para 2023…")
    # resolve against earlier turns. Defaults to empty for stateless callers.
    chat_history: list[dict[str, str]] = Field(default_factory=list)


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    facts: list[Fact]
    missing_facts: list[MissingFact]
    candidate_obligation_ids: list[str]
    # Full candidate objects (title/risk/confidence/evidence). The web UI
    # renders these directly; `candidate_obligation_ids` stays for
    # backward-compatible consumers that only need the ids.
    obligations: list[ObligationCandidate] = Field(default_factory=list)
    next_questions: list[str]
    degraded: bool = False
    degraded_facts: list[str] = Field(default_factory=list)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "ts": datetime.now(UTC).isoformat()}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus-format HTTP metrics (request counts, errors, latency).

    Exempt from auth and rate limiting so scrapers always reach it.
    """
    return Response(
        content=app.state.api_metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4",
    )


@app.post("/cases", response_model=CaseState, dependencies=[Depends(require_api_key)])
def create_case(
    payload: CreateCaseRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> CaseState:
    now = datetime.now(UTC)
    case = CaseState(
        case_id=f"case_{uuid4().hex}",
        user_id=payload.user_id,
        jurisdiction=payload.jurisdiction,
        tax_period=payload.tax_period,
        created_at=now,
        updated_at=now,
    )
    store.save_case(case)
    store.append_audit_event(case.case_id, {"event_type": "case_created"})
    return case


@app.get("/cases/{case_id}", response_model=CaseState, dependencies=[Depends(require_api_key)])
def get_case(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> CaseState:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


@app.get("/cases/{case_id}/audit", dependencies=[Depends(require_api_key)])
def get_case_audit(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> dict:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return {"case_id": case_id, "events": store.list_audit_events(case_id)}


@app.post("/cases/{case_id}/turn", response_model=TurnResponse, dependencies=[Depends(require_api_key)])
def post_turn(
    case_id: str,
    payload: TurnRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    extractor: Annotated[FactExtractor, Depends(get_fact_extractor)],
) -> TurnResponse:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")

    try:
        outcome = process_turn(
            case=case,
            user_input=payload.user_input,
            extractor=extractor,
            chat_history=payload.chat_history,
        )
    except OpenAIExtractionError as exc:
        # The fact extractor (LLM) failed after its own retries. Surface a clean
        # 503 instead of an opaque 500 — and never silently fall back to the
        # weaker regex extractor, which could persist wrong fiscal facts.
        raise HTTPException(
            status_code=503,
            detail=f"No se pudieron extraer los hechos de la consulta: {exc}",
        ) from exc

    try:
        updated = store.save_case(outcome.case_state)
    except CaseVersionConflictError as exc:
        # Another turn updated this case between our read and write. Ask the
        # client to retry against the fresh state rather than clobber it.
        raise HTTPException(
            status_code=409,
            detail="El caso se modificó concurrentemente; vuelve a cargarlo y reintenta.",
        ) from exc

    # execute planner for side effect of validation and auditability
    Planner().plan(updated, outcome.rules_result.candidate_obligations)

    store.append_audit_event(case_id, {"event_type": "turn_processed", "input": payload.user_input})
    store.append_audit_event(
        case_id,
        build_recommendation_audit_event(
            case_state=updated,
            interpretation=outcome.interpretation,
            rules_result=outcome.rules_result,
            obligations=outcome.rules_result.candidate_obligations,
        ),
    )

    return TurnResponse(
        case_id=case_id,
        facts=updated.facts,
        missing_facts=updated.missing_facts,
        candidate_obligation_ids=[o.obligation_id for o in updated.obligation_candidates],
        obligations=list(updated.obligation_candidates),
        next_questions=[q.question_text for q in outcome.selected_questions],
        degraded=bool(updated.gave_up_facts),
        degraded_facts=list(updated.gave_up_facts),
    )


class QARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    chat_history: list[dict] = Field(default_factory=list)


@_thread_safe_singleton
def _build_qa_chain():
    """Build the retrieval chain once and reuse it across requests.

    Constructing the chain loads the local embedding model (Qwen3-Embedding,
    several GB) and the FAISS index. Doing that on every ``/qa`` request — as
    the previous per-request dependency did — added seconds of latency and
    re-loaded the model into memory each time. The cache makes the first
    request pay the cost and the rest reuse the warm chain.
    """
    from hacienda_gpt.llm.chain import create_openai_chain
    from hacienda_gpt.utils import get_openai_api_key

    return create_openai_chain(openai_api_key=get_openai_api_key())


def get_qa_chain():
    """FastAPI dependency returning the cached retrieval chain.

    Translates a failed chain build (missing OPENAI_API_KEY, absent FAISS index,
    a model that won't load) into a clean 503 instead of an opaque 500. The
    failure is cached by ``_build_qa_chain`` so repeated requests fail fast
    rather than re-running the expensive build. Kept as a thin wrapper so tests
    can still override it via ``app.dependency_overrides``.
    """
    try:
        return _build_qa_chain()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"El servicio de consulta no está disponible: {exc}",
        ) from exc


@app.post("/qa", response_model=AnswerEnvelope, dependencies=[Depends(require_api_key)])
def post_qa(payload: QARequest, chain=Depends(get_qa_chain)) -> AnswerEnvelope:
    from hacienda_gpt.llm.chain import answer_with_grounding

    try:
        return answer_with_grounding(chain, query=payload.query, chat_history=payload.chat_history)
    except HTTPException:
        raise
    except Exception as exc:
        # The chain built fine but answering failed (retrieval, the OpenAI call,
        # grounding…). Surface a clean error instead of an opaque 500. This is
        # per-request, so — unlike a build failure — it is NOT cached.
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo procesar la consulta: {exc}",
        ) from exc


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    chat_history: list[dict] = Field(default_factory=list)
    # Optional: attach a case so the agent can read/update the user's fiscal
    # facts and run the rules engine. Without it the agent only searches.
    case_id: str | None = None


class AgentResponse(AgentResult):
    case_id: str | None = None
    # True when a tool changed the case and the change was persisted.
    case_updated: bool = False
    # Candidate obligations on the attached case after this turn (empty without a case).
    obligations: list[ObligationCandidate] = Field(default_factory=list)
    # What the advisor knows about the user after this turn, so the UI can show
    # (and let the user correct) the facts the answer was based on.
    facts: list[Fact] = Field(default_factory=list)
    tax_period: str | None = None


@_thread_safe_singleton
def _build_agent() -> TaxAgent:
    """Build the agent once (loads the embedder + FAISS index, like the QA chain)."""
    from hacienda_gpt.agent.agent import build_agent

    return build_agent()


def get_agent() -> TaxAgent:
    """FastAPI dependency returning the cached agent; a failed build is a 503.

    Kept as a thin wrapper so tests can override it via ``app.dependency_overrides``.
    """
    try:
        return _build_agent()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"El agente no está disponible: {exc}",
        ) from exc


def _load_agent_case(case_id: str | None, store: SQLiteCaseStateStore) -> CaseState | None:
    if case_id is None:
        return None
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


def _finish_agent_turn(
    payload: AgentRequest,
    case: CaseState | None,
    result: AgentResult,
    store: SQLiteCaseStateStore,
) -> AgentResponse:
    """Persist what the agent changed on the case, audit the turn, build the response."""
    case_updated = False
    if payload.case_id is not None:
        if result.updated_case is not None:
            try:
                store.save_case(result.updated_case)
            except CaseVersionConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="El caso se modificó concurrentemente; vuelve a cargarlo y reintenta.",
                ) from exc
            case_updated = True
        store.append_audit_event(
            payload.case_id,
            {
                "event_type": "agent_answer",
                "input": payload.query,
                "mode": result.mode.value,
                "provider": result.provider,
                "model": result.model,
                "tool_trace": [entry.model_dump(mode="json") for entry in result.tool_trace],
                "citations": [{"fragment_id": c.fragment_id, "locator": c.locator} for c in result.citations],
                "claims": [claim.model_dump(mode="json") for claim in result.claims],
                "warnings": result.warnings,
            },
        )

    final_case = result.updated_case or case
    return AgentResponse(
        **result.model_dump(),
        case_id=payload.case_id,
        case_updated=case_updated,
        obligations=list(final_case.obligation_candidates) if final_case is not None else [],
        facts=list(final_case.facts) if final_case is not None else [],
        tax_period=final_case.tax_period if final_case is not None else None,
    )


@app.post("/agent", response_model=AgentResponse, dependencies=[Depends(require_api_key)])
def post_agent(
    payload: AgentRequest,
    agent: Annotated[TaxAgent, Depends(get_agent)],
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> AgentResponse:
    """Tool-using advisor: searches the corpus, cites fragments, verifies claims.

    Unlike ``/qa`` (fixed retrieve-then-answer chain), the model decides what to
    look up and how often, and every normative claim in the answer is checked
    against the fragment it cites before the response is labelled ``verified``.
    """
    case = _load_agent_case(payload.case_id, store)
    try:
        result = agent.run(payload.query, chat_history=payload.chat_history, case=case)
    except ProviderError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo procesar la consulta: {exc}",
        ) from exc
    return _finish_agent_turn(payload, case, result, store)


# Seconds between keep-alive lines while the model is working, so proxies and
# the browser's idle timeout do not cut a long (but healthy) run.
_STREAM_HEARTBEAT_SECONDS = 10.0


@app.post("/agent/stream", dependencies=[Depends(require_api_key)])
def post_agent_stream(
    payload: AgentRequest,
    agent: Annotated[TaxAgent, Depends(get_agent)],
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> StreamingResponse:
    """Same as ``/agent``, streamed as NDJSON so the UI can show live progress.

    One JSON object per line: ``{"event": "progress", "data": {...}}`` while the
    agent works (searches issued, verification, revision), ``{"event": "ping"}``
    keep-alives, then exactly one terminal line — ``{"event": "result", "data":
    <AgentResponse>}`` or ``{"event": "error", "data": {"status", "detail"}}``.
    Request-level failures that happen before the run starts (404 case, 503
    agent unavailable, 401) are still plain HTTP errors.
    """
    case = _load_agent_case(payload.case_id, store)
    events: queue.Queue[dict] = queue.Queue()

    def work() -> None:
        try:
            result = agent.run(
                payload.query,
                chat_history=payload.chat_history,
                case=case,
                on_event=lambda event: events.put({"event": "progress", "data": event}),
            )
            response = _finish_agent_turn(payload, case, result, store)
            events.put({"event": "result", "data": response.model_dump(mode="json")})
        except ProviderError as exc:
            events.put({"event": "error", "data": {"status": 503, "detail": f"No se pudo procesar la consulta: {exc}"}})
        except HTTPException as exc:
            events.put({"event": "error", "data": {"status": exc.status_code, "detail": str(exc.detail)}})
        except Exception as exc:  # the stream must always end with a terminal line
            events.put({"event": "error", "data": {"status": 500, "detail": f"Error interno: {exc}"}})

    def lines():
        threading.Thread(target=work, name="agent-stream", daemon=True).start()
        while True:
            try:
                event = events.get(timeout=_STREAM_HEARTBEAT_SECONDS)
            except queue.Empty:
                yield json.dumps({"event": "ping"}) + "\n"
                continue
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
            if event["event"] in ("result", "error"):
                return

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        # Tell reverse proxies (nginx) not to buffer: progress must arrive live.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _save_case_after_user_edit(
    case: CaseState, facts: list[Fact], store: SQLiteCaseStateStore, audit_event: dict, tax_period: str | None = None
) -> CaseState:
    """Persist a user-driven change to the case facts and re-evaluate obligations."""
    known = {fact.name for fact in facts}
    edited = case.model_copy(
        update={
            "facts": facts,
            "tax_period": tax_period or case.tax_period,
            # CaseState forbids a fact being both known and missing.
            "missing_facts": [m for m in case.missing_facts if m.fact_name not in known],
            "updated_at": datetime.now(UTC),
        }
    )
    rules_result = evaluate_rules(case_state=edited, recent_facts=[])
    edited = edited.model_copy(update={"obligation_candidates": rules_result.candidate_obligations})
    try:
        updated = store.save_case(edited)
    except CaseVersionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="El caso se modificó concurrentemente; vuelve a cargarlo y reintenta.",
        ) from exc
    store.append_audit_event(case.case_id, audit_event)
    return updated


class FactUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | float | bool


@app.put("/cases/{case_id}/facts/{fact_name}", response_model=CaseState, dependencies=[Depends(require_api_key)])
def put_case_fact(
    case_id: str,
    fact_name: str,
    payload: FactUpdateRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> CaseState:
    """Set one fact of a case to a value the user typed — correcting what the
    advisor recorded, or filling it in directly. The value is coerced to the
    fact's type exactly like an extracted one; obligations are re-evaluated."""
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    expected = EXPECTED_FACT_TYPE.get(fact_name)
    if expected is None:
        raise HTTPException(status_code=404, detail="fact not found")

    raw = payload.value
    value = _coerce_value(
        expected=expected,
        value_string=raw if isinstance(raw, str) else None,
        value_number=raw if isinstance(raw, int | float) and not isinstance(raw, bool) else None,
        value_boolean=raw if isinstance(raw, bool) else None,
    )
    if value is None:
        raise HTTPException(
            status_code=422, detail=f"El valor no es válido para este dato (se espera {expected.value})."
        )
    tax_period: str | None = None
    if fact_name == "periodo_fiscal":
        year = parse_fiscal_year(str(value))
        if year is None or not 1990 <= year <= datetime.now(UTC).year + 1:
            raise HTTPException(status_code=422, detail="El ejercicio debe ser un año de cuatro cifras.")
        value = tax_period = str(year)

    fact = Fact(
        fact_id=f"fact_{fact_name}",
        name=fact_name,
        value=value,
        value_type=expected,
        source="user_edit",
        confidence=1.0,
    )
    facts = [*(existing for existing in case.facts if existing.name != fact_name), fact]
    return _save_case_after_user_edit(
        case,
        facts,
        store,
        {"event_type": "fact_edited_by_user", "fact_name": fact_name, "value": value},
        tax_period=tax_period,
    )


@app.delete("/cases/{case_id}/facts/{fact_name}", response_model=CaseState, dependencies=[Depends(require_api_key)])
def delete_case_fact(
    case_id: str,
    fact_name: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> CaseState:
    """Remove one fact from a case. Obligations are re-evaluated without it."""
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    remaining = [fact for fact in case.facts if fact.name != fact_name]
    if len(remaining) == len(case.facts):
        raise HTTPException(status_code=404, detail="fact not found")
    return _save_case_after_user_edit(
        case, remaining, store, {"event_type": "fact_removed_by_user", "fact_name": fact_name}
    )


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # With a case, its facts are the base; `datos` adds or overrides figures.
    case_id: str | None = None
    ejercicio: int | None = None
    datos: dict = Field(default_factory=dict)
    escenarios: list[dict] | None = None
    # Evaluate the benefit catalogue on the last scenario instead of the base
    # one (what an interactive simulator shows while the user edits figures).
    beneficios_sobre_ultimo: bool = False


@app.post("/simulate", response_model=Simulacion, dependencies=[Depends(require_api_key)])
def post_simulate(
    payload: SimulateRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> Simulacion:
    """Deterministic IRPF simulation — no LLM involved, so it is instant and free.

    Returns the base scenario, each alternative with its saving versus the
    base, and the tax-benefit catalogue checked against the profile.
    """
    case = _load_agent_case(payload.case_id, store)
    facts = {fact.name: fact.value for fact in case.facts} if case is not None else {}
    year = payload.ejercicio
    if year is None:
        year = (parse_fiscal_year(case.tax_period) if case is not None else None) or max(ejercicios_disponibles())
    try:
        return simular(
            ejercicio=year,
            facts=facts,
            datos=payload.datos,
            escenarios=payload.escenarios,
            beneficios_sobre_ultimo=payload.beneficios_sobre_ultimo,
        )
    except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class CalendarResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    hoy: date
    vencimientos: list[Vencimiento]
    aviso: str


@app.get("/cases/{case_id}/calendar", response_model=CalendarResponse, dependencies=[Depends(require_api_key)])
def get_case_calendar(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    dias: int = 365,
) -> CalendarResponse:
    """Upcoming filing deadlines that apply to this case's profile."""
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    today = datetime.now(UTC).date()
    facts = {fact.name: fact.value for fact in case.facts}
    return CalendarResponse(
        case_id=case_id,
        hoy=today,
        vencimientos=proximos_vencimientos(facts, hoy=today, dias=max(1, min(730, dias))),
        aviso=aviso_festivos(),
    )


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=200)
    # Base64 of the file. JSON instead of multipart keeps the API dependency-free.
    content_base64: str = Field(min_length=1)


@_thread_safe_singleton
def _build_document_provider() -> LLMProvider:
    from hacienda_gpt import settings
    from hacienda_gpt.agent.providers import create_provider

    # Locating labelled figures in a document is a constrained task: low effort.
    return create_provider(effort=settings.DECISION_EXTRACTOR_EFFORT)


def get_document_provider() -> LLMProvider:
    """FastAPI dependency (overridable in tests); a failed build is a 503."""
    try:
        return _build_document_provider()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"El lector de documentos no está disponible: {exc}") from exc


@app.post("/cases/{case_id}/documents", response_model=AnalisisDocumento, dependencies=[Depends(require_api_key)])
def post_case_document(
    case_id: str,
    payload: DocumentRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    provider: Annotated[LLMProvider, Depends(get_document_provider)],
) -> AnalisisDocumento:
    """Read one of the user's tax documents and PROPOSE profile facts from it.

    Nothing is written to the case: the client shows the proposals and applies
    the ones the user accepts through ``PUT /cases/{id}/facts/{name}``. The
    document is processed in memory and not stored; its text is sent to the
    configured LLM provider. Only the file name and the proposed fact names are
    audited.
    """
    if store.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    try:
        data = decode_upload(payload.content_base64)
        analysis = analizar_documento(provider, filename=payload.filename, data=data)
    except DocumentoNoLegibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"No se pudo analizar el documento: {exc}") from exc
    store.append_audit_event(
        case_id,
        {
            "event_type": "document_analyzed",
            "filename": payload.filename,
            "document_type": analysis.tipo_documento,
            "proposed_facts": [fact.name for fact in analysis.hechos_propuestos],
        },
    )
    return analysis


@_thread_safe_singleton
def _build_notification_store() -> NotificationStore:
    # Same SQLite file as the cases, so one backup covers both.
    return NotificationStore(DECISION_STATE_DB_PATH)


def get_notification_store() -> NotificationStore:
    # Thin wrapper so tests can override it via app.dependency_overrides.
    return _build_notification_store()


class NotificationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    unread: int
    notifications: list[Notification]


class ProactiveRunResponse(NotificationsResponse):
    # Notifications created by this run, per kind.
    created: dict[str, int]


@app.get(
    "/cases/{case_id}/notifications", response_model=NotificationsResponse, dependencies=[Depends(require_api_key)]
)
def get_case_notifications(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    notifications: Annotated[NotificationStore, Depends(get_notification_store)],
    unread_only: bool = False,
) -> NotificationsResponse:
    """Notifications the proactive jobs wrote for this case, newest first."""
    if store.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    return NotificationsResponse(
        case_id=case_id,
        unread=notifications.count_unread(case_id),
        notifications=notifications.list(case_id, unread_only=unread_only),
    )


@app.post(
    "/cases/{case_id}/notifications/{notification_id}/read",
    response_model=NotificationsResponse,
    dependencies=[Depends(require_api_key)],
)
def post_notification_read(
    case_id: str,
    notification_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    notifications: Annotated[NotificationStore, Depends(get_notification_store)],
) -> NotificationsResponse:
    if store.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="case not found")
    if not notifications.mark_read(case_id, notification_id):
        raise HTTPException(status_code=404, detail="notification not found")
    return NotificationsResponse(
        case_id=case_id, unread=notifications.count_unread(case_id), notifications=notifications.list(case_id)
    )


@app.post(
    "/cases/{case_id}/proactive/run", response_model=ProactiveRunResponse, dependencies=[Depends(require_api_key)]
)
def post_case_proactive_run(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    notifications: Annotated[NotificationStore, Depends(get_notification_store)],
) -> ProactiveRunResponse:
    """Run the deterministic proactive jobs (deadlines + year-end plan) for this case now. No LLM."""
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    created = ejecutar_para_caso(case, hoy=hoy_en_espana(), store=notifications)
    return ProactiveRunResponse(
        case_id=case_id,
        unread=notifications.count_unread(case_id),
        notifications=notifications.list(case_id),
        created=created,
    )


@app.get("/cases/{case_id}/audit/export", dependencies=[Depends(require_api_key)])
def export_case_audit(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> dict:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    events = store.list_audit_events(case_id)
    return {"case_id": case_id, "exported_at": datetime.now(UTC).isoformat(), "events": events}
