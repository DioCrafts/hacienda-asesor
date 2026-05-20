from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, MissingFact, RiskLevel


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


def _detect_intent(text: str) -> tuple[Intent, float]:
    if re.search(r"\birpf\b|\brenta\b|declaraci[oó]n de la renta", text):
        return Intent.DECLARACION_IRPF, 0.84
    if re.search(r"\biva\b|modelo\s+303|modelo\s+390", text):
        return Intent.IVA, 0.84
    if re.search(r"aut[oó]nomo|autonoma|autónoma|alta en reta|cuota de autónomos", text):
        return Intent.AUTONOMO, 0.81
    if re.search(r"impuesto|hacienda|aeat|tribut", text):
        return Intent.GENERIC_TRIBUTARY, 0.62
    return Intent.UNKNOWN, 0.3


def _extract_facts(text: str, current_case_state: CaseState | None) -> list[Fact]:
    facts: dict[str, Fact] = {}

    if current_case_state is not None:
        for fact in current_case_state.facts:
            facts[fact.fact_id] = fact

    if any(token in text for token in ["residente en españa", "residencia fiscal en españa", "vivo en españa"]):
        facts["fact_residencia_fiscal"] = Fact(
            fact_id="fact_residencia_fiscal",
            name="residencia_fiscal",
            value="ES",
            value_type=FactValueType.STRING,
            source="user_input",
            confidence=0.82,
        )

    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        facts["fact_tax_year"] = Fact(
            fact_id="fact_tax_year",
            name="periodo_fiscal",
            value=year_match.group(1),
            value_type=FactValueType.STRING,
            source="user_input",
            confidence=0.77,
        )

    if "ingres" in text or "rendimiento" in text:
        facts["fact_income_mentioned"] = Fact(
            fact_id="fact_income_mentioned",
            name="menciona_ingresos",
            value=True,
            value_type=FactValueType.BOOLEAN,
            source="user_input",
            confidence=0.75,
        )

    return list(facts.values())


def interpret_turn(
    user_input: str,
    chat_history: list[dict[str, str]] | list[str],
    current_case_state: CaseState | None,
) -> InterpretationResult:
    """Interpret user turn into structured intent/facts/uncertainties/questions.

    This component does not emit final fiscal recommendations.
    """

    del chat_history  # reserved for future context-aware extraction
    text = user_input.lower().strip()
    intent, intent_conf = _detect_intent(text)
    facts = _extract_facts(text, current_case_state)

    missing_facts: list[MissingFact] = []
    uncertainties: list[Uncertainty] = []
    next_questions: list[QuestionPrompt] = []

    fact_names = {fact.name for fact in facts}
    if "menciona_ingresos" not in fact_names:
        missing_facts.append(
            MissingFact(
                fact_name="tipo_renta",
                reason="falta_clasificacion_de_ingresos",
                priority=RiskLevel.HIGH,
            )
        )
        next_questions.append(
            QuestionPrompt(
                question_id="q_tipo_renta",
                question_text="¿Qué tipo de ingresos has tenido (trabajo, actividad económica, capital u otros)?",
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
        next_questions.append(
            QuestionPrompt(
                question_id="q_residencia",
                question_text="¿Cuál es tu residencia fiscal actual?",
                target_fact="residencia_fiscal",
                reason="requerido_para_determinar_jurisdiccion",
                priority=RiskLevel.HIGH,
            )
        )

    if intent is Intent.UNKNOWN:
        uncertainties.append(
            Uncertainty(
                code="intent_ambiguous",
                message="No se pudo identificar con suficiente claridad la intención fiscal principal.",
                severity=RiskLevel.MEDIUM,
            )
        )

    if not re.search(r"\b(20\d{2})\b", text):
        uncertainties.append(
            Uncertainty(
                code="tax_period_missing",
                message="No se identificó el período fiscal explícito en el turno.",
                severity=RiskLevel.LOW,
            )
        )

    return InterpretationResult(
        intent=intent,
        confidence=intent_conf,
        extracted_facts=facts,
        uncertainties=uncertainties,
        missing_facts=missing_facts,
        next_questions=next_questions,
    )
