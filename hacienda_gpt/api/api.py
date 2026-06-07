from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, TypeVar
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

from hacienda_gpt.decision.audit import build_recommendation_audit_event
from hacienda_gpt.decision.fact_extractor import FactExtractor, default_fact_extractor
from hacienda_gpt.decision.planner import Planner
from hacienda_gpt.decision.schemas import (
    CaseState,
    Fact,
    MissingFact,
    ObligationCandidate,
)
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.decision.turn_service import process_turn
from hacienda_gpt.llm.grounding import AnswerEnvelope
from hacienda_gpt.settings import DECISION_STATE_DB_PATH

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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


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


@app.post("/cases", response_model=CaseState)
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


@app.get("/cases/{case_id}", response_model=CaseState)
def get_case(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> CaseState:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


@app.get("/cases/{case_id}/audit")
def get_case_audit(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> dict:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return {"case_id": case_id, "events": store.list_audit_events(case_id)}


@app.post("/cases/{case_id}/turn", response_model=TurnResponse)
def post_turn(
    case_id: str,
    payload: TurnRequest,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
    extractor: Annotated[FactExtractor, Depends(get_fact_extractor)],
) -> TurnResponse:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")

    outcome = process_turn(
        case=case,
        user_input=payload.user_input,
        extractor=extractor,
        chat_history=payload.chat_history,
    )
    updated = outcome.case_state
    store.save_case(updated)

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


@app.post("/qa", response_model=AnswerEnvelope)
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


@app.get("/cases/{case_id}/audit/export")
def export_case_audit(
    case_id: str,
    store: Annotated[SQLiteCaseStateStore, Depends(get_case_store)],
) -> dict:
    case = store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    events = store.list_audit_events(case_id)
    return {"case_id": case_id, "exported_at": datetime.now(UTC).isoformat(), "events": events}
