"""Shared turn-processing pipeline for the decision engine.

Both the FastAPI ``/cases/{id}/turn`` endpoint and the Streamlit UI need to turn
a user message into an updated :class:`CaseState`: extract facts, resolve the
fiscal period, run the rules engine and apply the :class:`QuestionPolicy`
(``ask_counts`` / ``gave_up_facts`` / ``asked_facts``). Both used to inline this
logic, and the UI silently skipped the question policy, so the two paths drifted
apart. This module is the single implementation both callers delegate to;
persistence and auditing stay with each caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from hacienda_gpt.decision.fact_extractor import FactExtractor
from hacienda_gpt.decision.interpreter import InterpretationResult, QuestionPrompt, interpret_turn
from hacienda_gpt.decision.question_policy import QuestionPolicy
from hacienda_gpt.decision.rules_engine import RulesEngineResult, evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact
from hacienda_gpt.decision.taxonomy import SupportedIntent


@dataclass(frozen=True)
class TurnOutcome:
    """Everything a caller needs to persist, audit and render a turn."""

    case_state: CaseState
    interpretation: InterpretationResult
    rules_result: RulesEngineResult
    selected_questions: list[QuestionPrompt]


def resolve_tax_period(case: CaseState, facts: list[Fact]) -> str:
    """Return the fiscal period the user mentioned this turn, else the case's."""
    period_fact = next((fact for fact in facts if fact.name == "periodo_fiscal"), None)
    if period_fact is None:
        return case.tax_period
    return str(period_fact.value)


def _to_supported_intent(intent_value: str) -> SupportedIntent:
    try:
        return SupportedIntent(intent_value)
    except ValueError:
        return SupportedIntent.UNKNOWN


def process_turn(
    *,
    case: CaseState,
    user_input: str,
    extractor: FactExtractor,
    chat_history: list[dict[str, str]] | list[str] | None = None,
    max_questions: int = 1,
) -> TurnOutcome:
    """Run the full interpret → rules → question-policy pipeline for one turn.

    The returned :class:`CaseState` already carries the merged facts, resolved
    tax period, candidate obligations and the updated question-policy
    bookkeeping. Callers are responsible for saving it and emitting audit
    events.
    """
    interpretation = interpret_turn(
        user_input,
        chat_history=chat_history or [],
        current_case_state=case,
        extractor=extractor,
    )

    effective_tax_period = resolve_tax_period(case, interpretation.extracted_facts)
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

    intent = _to_supported_intent(interpretation.intent.value)
    policy_result = QuestionPolicy().select_next_questions(
        case_state=updated,
        intent=intent,
        candidate_questions=interpretation.next_questions,
        max_questions=max_questions,
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

    return TurnOutcome(
        case_state=updated,
        interpretation=interpretation,
        rules_result=rules_result,
        selected_questions=selected_questions,
    )
