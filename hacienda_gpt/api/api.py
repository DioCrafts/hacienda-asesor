from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.audit import build_recommendation_audit_event
from hacienda_gpt.decision.fact_extractor import FactExtractor, default_fact_extractor
from hacienda_gpt.decision.interpreter import interpret_turn
from hacienda_gpt.decision.planner import Planner
from hacienda_gpt.decision.question_policy import QuestionPolicy
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact, MissingFact
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.decision.taxonomy import SupportedIntent
from hacienda_gpt.llm.grounding import AnswerEnvelope

app = FastAPI(title="HaciendaGPT Decision API", version="1.0.0")


def get_case_store() -> SQLiteCaseStateStore:
    return SQLiteCaseStateStore(str(Path("./data/api_case_state.sqlite3")))


def get_fact_extractor() -> FactExtractor:
    return default_fact_extractor()


def _resolve_tax_period(case: CaseState, facts: list[Fact]) -> str:
    period_fact = next((fact for fact in facts if fact.name == "periodo_fiscal"), None)
    if period_fact is None:
        return case.tax_period
    return str(period_fact.value)


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    jurisdiction: str = Field(default="ES", min_length=2)
    tax_period: str = Field(min_length=4)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_input: str = Field(min_length=1)


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    facts: list[Fact]
    missing_facts: list[MissingFact]
    candidate_obligation_ids: list[str]
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

    interpretation = interpret_turn(
        payload.user_input,
        chat_history=[],
        current_case_state=case,
        extractor=extractor,
    )
    effective_tax_period = _resolve_tax_period(case, interpretation.extracted_facts)
    case_for_rules = case.model_copy(update={"tax_period": effective_tax_period})
    rules_result = evaluate_rules(case_state=case_for_rules, recent_facts=interpretation.extracted_facts)

    updated = case.model_copy(
        update={
            "facts": interpretation.extracted_facts,
            "tax_period": effective_tax_period,
            "missing_facts": interpretation.missing_facts,
            "obligation_candidates": rules_result.candidate_obligations,
            "updated_at": datetime.now(UTC),
        }
    )

    intent = (
        SupportedIntent(interpretation.intent.value)
        if interpretation.intent.value in [e.value for e in SupportedIntent]
        else SupportedIntent.UNKNOWN
    )
    policy_result = QuestionPolicy().select_next_questions(
        case_state=updated,
        intent=intent,
        candidate_questions=interpretation.next_questions,
        max_questions=1,
    )
    selected_questions = policy_result.selected_questions

    new_ask_counts = dict(updated.ask_counts)
    for question in selected_questions:
        new_ask_counts[question.target_fact] = new_ask_counts.get(question.target_fact, 0) + 1

    merged_gave_up = list(dict.fromkeys([*updated.gave_up_facts, *policy_result.newly_gave_up]))
    merged_asked = list(dict.fromkeys([*updated.asked_facts, *(q.target_fact for q in selected_questions)]))
    missing_after = [m for m in updated.missing_facts if m.fact_name not in merged_gave_up]

    updated = updated.model_copy(
        update={
            "asked_facts": merged_asked,
            "ask_counts": new_ask_counts,
            "gave_up_facts": merged_gave_up,
            "missing_facts": missing_after,
        }
    )
    store.save_case(updated)

    # execute planner for side effect of validation and auditability
    Planner().plan(updated, rules_result.candidate_obligations)

    store.append_audit_event(case_id, {"event_type": "turn_processed", "input": payload.user_input})
    store.append_audit_event(
        case_id,
        build_recommendation_audit_event(
            case_state=updated,
            interpretation=interpretation,
            rules_result=rules_result,
            obligations=rules_result.candidate_obligations,
        ),
    )

    return TurnResponse(
        case_id=case_id,
        facts=updated.facts,
        missing_facts=updated.missing_facts,
        candidate_obligation_ids=[o.obligation_id for o in updated.obligation_candidates],
        next_questions=[q.question_text for q in selected_questions],
        degraded=bool(updated.gave_up_facts),
        degraded_facts=list(updated.gave_up_facts),
    )


class QARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    chat_history: list[dict] = Field(default_factory=list)


def get_qa_chain():
    """Lazily build the retrieval chain.

    Declared as a FastAPI dependency so tests can override it without
    needing OpenAI credentials or a real FAISS index.
    """
    from hacienda_gpt.llm.chain import create_openai_chain
    from hacienda_gpt.utils import get_openai_api_key

    return create_openai_chain(openai_api_key=get_openai_api_key())


@app.post("/qa", response_model=AnswerEnvelope)
def post_qa(payload: QARequest, chain=Depends(get_qa_chain)) -> AnswerEnvelope:
    from hacienda_gpt.llm.chain import answer_with_grounding

    return answer_with_grounding(chain, query=payload.query, chat_history=payload.chat_history)


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
