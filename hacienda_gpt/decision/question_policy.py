from __future__ import annotations

from dataclasses import dataclass

from hacienda_gpt.decision.interpreter import QuestionPrompt
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.taxonomy import SupportedIntent, blocking_facts_for_intent


@dataclass(frozen=True)
class QuestionPolicyResult:
    selected_questions: list[QuestionPrompt]


class QuestionPolicy:
    """Minimize user friction by asking only high-value, non-redundant questions."""

    def select_next_questions(
        self,
        case_state: CaseState,
        intent: SupportedIntent,
        candidate_questions: list[QuestionPrompt],
        max_questions: int = 1,
    ) -> QuestionPolicyResult:
        critical = set(blocking_facts_for_intent(intent))
        known_facts = {fact.name for fact in case_state.facts}
        asked_facts = set(case_state.asked_facts)

        available = [
            q
            for q in candidate_questions
            if q.target_fact not in known_facts and q.target_fact not in asked_facts
        ]

        critical_available = [q for q in available if q.target_fact in critical]
        pool = critical_available if critical_available else available

        ranked = sorted(pool, key=lambda q: self._information_gain_score(q, critical), reverse=True)
        return QuestionPolicyResult(selected_questions=ranked[:max_questions])

    def _information_gain_score(self, question: QuestionPrompt, critical_facts: set[str]) -> float:
        # Heuristic: critical fact coverage + priority bump.
        base = 1.0 if question.target_fact in critical_facts else 0.0
        priority_bonus = {
            "critical": 1.0,
            "high": 0.8,
            "medium": 0.5,
            "low": 0.2,
        }.get(question.priority.value, 0.0)
        return base + priority_bonus
