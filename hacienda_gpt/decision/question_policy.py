from __future__ import annotations

from dataclasses import dataclass, field

from hacienda_gpt.decision.interpreter import QuestionPrompt
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.taxonomy import SupportedIntent, blocking_facts_for_intent


@dataclass(frozen=True)
class QuestionPolicyResult:
    selected_questions: list[QuestionPrompt]
    newly_gave_up: list[str] = field(default_factory=list)


class QuestionPolicy:
    """Minimize user friction by asking only high-value, non-redundant questions.

    Behaviour:
    - Facts already known on the case are not asked.
    - Facts explicitly listed in `case_state.gave_up_facts` are not asked.
    - Facts whose `ask_counts` already hit `MAX_ASKS_PER_FACT` are added to
      `newly_gave_up` and skipped; the caller is expected to persist this list
      into `case_state.gave_up_facts` so that the conversation degrades to a
      partial answer instead of looping forever.
    - Otherwise we rephrase the question using `REPHRASING_TABLE` whenever
      `ask_counts[fact] > 0`, so the user gets a fresh wording on each retry.
    """

    MAX_ASKS_PER_FACT = 3

    REPHRASING_TABLE: dict[str, list[str]] = {
        "tipo_renta": [
            "¿Qué tipo de ingresos has tenido (trabajo, actividad económica, capital u otros)?",
            "Para poder ayudarte mejor: ¿tus ingresos vienen de un sueldo, de actividad económica (autónomo, empresa), de alquileres o de inversiones?",
            "Último intento sobre tus ingresos: indica la fuente principal — trabajo por cuenta ajena, autónomo, capital mobiliario, inmuebles o ganancias patrimoniales.",
        ],
        "residencia_fiscal": [
            "¿Cuál es tu residencia fiscal actual?",
            "¿Vives habitualmente en España? Si es así, ¿más de 183 días al año?",
            "Para precisar tu residencia fiscal: ¿en qué país pasas la mayor parte del año?",
        ],
        "periodo_fiscal": [
            "¿A qué ejercicio fiscal (año) se refiere tu consulta?",
            "¿De qué año o campaña fiscal hablamos exactamente?",
            "¿Podrías concretar el año del ejercicio (por ejemplo 2023, 2024)?",
        ],
    }

    def select_next_questions(
        self,
        case_state: CaseState,
        intent: SupportedIntent,
        candidate_questions: list[QuestionPrompt],
        max_questions: int = 1,
    ) -> QuestionPolicyResult:
        critical = set(blocking_facts_for_intent(intent))
        known_facts = {fact.name for fact in case_state.facts}
        gave_up = set(case_state.gave_up_facts)
        ask_counts = dict(case_state.ask_counts)

        newly_gave_up: list[str] = []

        for question in candidate_questions:
            target = question.target_fact
            if target in known_facts or target in gave_up:
                continue
            if ask_counts.get(target, 0) >= self.MAX_ASKS_PER_FACT and target not in newly_gave_up:
                newly_gave_up.append(target)

        already_giving_up = gave_up.union(newly_gave_up)

        eligible = [
            q
            for q in candidate_questions
            if q.target_fact not in known_facts and q.target_fact not in already_giving_up
        ]

        critical_available = [q for q in eligible if q.target_fact in critical]
        pool = critical_available if critical_available else eligible

        ranked = sorted(
            pool,
            key=lambda q: self._information_gain_score(q, critical),
            reverse=True,
        )
        selected = [self._maybe_rephrase(q, ask_counts.get(q.target_fact, 0)) for q in ranked[:max_questions]]
        return QuestionPolicyResult(selected_questions=selected, newly_gave_up=newly_gave_up)

    def _maybe_rephrase(self, question: QuestionPrompt, prior_ask_count: int) -> QuestionPrompt:
        if prior_ask_count <= 0:
            return question
        alternatives = self.REPHRASING_TABLE.get(question.target_fact)
        if not alternatives:
            return question
        index = min(prior_ask_count, len(alternatives) - 1)
        rephrased_text = alternatives[index]
        if rephrased_text == question.question_text:
            return question
        return question.model_copy(update={"question_text": rephrased_text})

    def _information_gain_score(self, question: QuestionPrompt, critical_facts: set[str]) -> float:
        base = 1.0 if question.target_fact in critical_facts else 0.0
        priority_bonus = {
            "critical": 1.0,
            "high": 0.8,
            "medium": 0.5,
            "low": 0.2,
        }.get(question.priority.value, 0.0)
        return base + priority_bonus
