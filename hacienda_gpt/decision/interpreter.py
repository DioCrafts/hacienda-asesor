from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hacienda_gpt.decision.fact_extractor import (
    FactExtractor,
    RegexFactExtractor,
)
from hacienda_gpt.decision.schemas import CaseState, Fact, MissingFact, RiskLevel


class Intent(str, Enum):
    DECLARACION_IRPF = "declaracion_irpf"
    IVA = "iva"
    AUTONOMO = "autonomo"
    GENERIC_TRIBUTARY = "generic_tributary"
    UNKNOWN = "unknown"


class Uncertainty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: RiskLevel


class QuestionPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)
    target_fact: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    priority: RiskLevel


class InterpretationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    extracted_facts: list[Fact] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    missing_facts: list[MissingFact] = Field(default_factory=list)
    next_questions: list[QuestionPrompt] = Field(default_factory=list)
    interpreted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_questions_for_missing(self) -> InterpretationResult:
        missing_names = {item.fact_name for item in self.missing_facts}
        for question in self.next_questions:
            if question.target_fact not in missing_names:
                raise ValueError(
                    f"question target_fact '{question.target_fact}' must exist in missing_facts"
                )
        return self


# Question templates used when an extractor surfaces a missing fact. The
# QuestionPolicy may reformulate them based on how many times the fact has
# already been asked.
_QUESTION_TEMPLATES: dict[str, tuple[str, str]] = {
    "tipo_renta": (
        "q_tipo_renta",
        "¿Qué tipo de ingresos has tenido (trabajo, actividad económica, capital u otros)?",
    ),
    "residencia_fiscal": (
        "q_residencia",
        "¿Cuál es tu residencia fiscal actual?",
    ),
    "periodo_fiscal": (
        "q_periodo",
        "¿A qué ejercicio fiscal (año) se refiere tu consulta?",
    ),
}


def _merge_facts(new_facts: list[Fact], current: CaseState | None) -> list[Fact]:
    """Merge facts coming from this turn with those already on the case.

    New facts win when they target the same logical name (same `fact_id` or
    same `name`). Anything not overridden is preserved.
    """

    merged: dict[str, Fact] = {}
    if current is not None:
        for fact in current.facts:
            merged[fact.fact_id] = fact

    by_name = {fact.name: fact for fact in merged.values()}

    for fact in new_facts:
        prior = by_name.get(fact.name)
        if prior is not None and prior.fact_id != fact.fact_id:
            # Replace the previous entry that used a different id but represents
            # the same logical fact.
            merged.pop(prior.fact_id, None)
        merged[fact.fact_id] = fact
        by_name[fact.name] = fact

    return list(merged.values())


def _build_missing_and_questions(
    intent: Intent,
    fact_names: set[str],
) -> tuple[list[MissingFact], list[QuestionPrompt]]:
    missing_facts: list[MissingFact] = []
    next_questions: list[QuestionPrompt] = []

    if "menciona_ingresos" not in fact_names and "tipo_renta" not in fact_names:
        missing_facts.append(
            MissingFact(
                fact_name="tipo_renta",
                reason="falta_clasificacion_de_ingresos",
                priority=RiskLevel.HIGH,
            )
        )
        qid, text = _QUESTION_TEMPLATES["tipo_renta"]
        next_questions.append(
            QuestionPrompt(
                question_id=qid,
                question_text=text,
                target_fact="tipo_renta",
                reason="requerido_para_analisis_fiscal",
                priority=RiskLevel.HIGH,
            )
        )

    if "residencia_fiscal" not in fact_names:
        missing_facts.append(
            MissingFact(
                fact_name="residencia_fiscal",
                reason="necesaria_para_contexto_jurisdiccional",
                priority=RiskLevel.HIGH,
            )
        )
        qid, text = _QUESTION_TEMPLATES["residencia_fiscal"]
        next_questions.append(
            QuestionPrompt(
                question_id=qid,
                question_text=text,
                target_fact="residencia_fiscal",
                reason="requerido_para_determinar_jurisdiccion",
                priority=RiskLevel.HIGH,
            )
        )

    if intent is not Intent.UNKNOWN and "periodo_fiscal" not in fact_names:
        # Soft missing: we won't always block on this, but signal it so the
        # policy can choose to ask after the higher-priority facts.
        missing_facts.append(
            MissingFact(
                fact_name="periodo_fiscal",
                reason="periodo_fiscal_no_identificado",
                priority=RiskLevel.MEDIUM,
            )
        )
        qid, text = _QUESTION_TEMPLATES["periodo_fiscal"]
        next_questions.append(
            QuestionPrompt(
                question_id=qid,
                question_text=text,
                target_fact="periodo_fiscal",
                reason="necesario_para_contextualizar_normativa",
                priority=RiskLevel.MEDIUM,
            )
        )

    return missing_facts, next_questions


def interpret_turn(
    user_input: str,
    chat_history: list[dict[str, str]] | list[str],
    current_case_state: CaseState | None,
    extractor: FactExtractor | None = None,
) -> InterpretationResult:
    """Interpret user turn into structured intent/facts/uncertainties/questions.

    The actual fact extraction is delegated to a `FactExtractor`. When no
    extractor is provided we fall back to the deterministic regex extractor so
    that existing tests and offline development keep working without
    contacting the LLM.
    """

    active_extractor = extractor or RegexFactExtractor()
    payload = active_extractor.extract(user_input, chat_history, current_case_state)

    try:
        intent = Intent(payload.intent)
    except ValueError:
        intent = Intent.UNKNOWN

    facts = _merge_facts(payload.extracted_facts, current_case_state)
    fact_names = {fact.name for fact in facts}

    missing_facts, next_questions = _build_missing_and_questions(intent, fact_names)
    gave_up = set(getattr(current_case_state, "gave_up_facts", []) or [])
    if gave_up:
        missing_facts = [m for m in missing_facts if m.fact_name not in gave_up]
        next_questions = [q for q in next_questions if q.target_fact not in gave_up]

    uncertainties: list[Uncertainty] = []
    if intent is Intent.UNKNOWN:
        uncertainties.append(
            Uncertainty(
                code="intent_ambiguous",
                message="No se pudo identificar con suficiente claridad la intención fiscal principal.",
                severity=RiskLevel.MEDIUM,
            )
        )

    if "periodo_fiscal" not in fact_names:
        uncertainties.append(
            Uncertainty(
                code="tax_period_missing",
                message="No se identificó el período fiscal explícito en el turno.",
                severity=RiskLevel.LOW,
            )
        )

    return InterpretationResult(
        intent=intent,
        confidence=payload.intent_confidence,
        extracted_facts=facts,
        uncertainties=uncertainties,
        missing_facts=missing_facts,
        next_questions=next_questions,
    )
