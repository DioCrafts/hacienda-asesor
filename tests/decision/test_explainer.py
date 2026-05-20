from datetime import UTC, datetime

from hacienda_gpt.decision.explainer import ExplanationComposer
from hacienda_gpt.decision.interpreter import Intent, InterpretationResult, QuestionPrompt, Uncertainty
from hacienda_gpt.decision.planner import PlannedAction, PlannerResult
from hacienda_gpt.decision.schemas import (
    ActionItem,
    CaseState,
    EvidenceRef,
    EvidenceSourceType,
    Fact,
    FactValueType,
    MissingFact,
    ObligationCandidate,
    RiskLevel,
)


def test_explainer_outputs_required_sections_and_traceability() -> None:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        missing_facts=[MissingFact(fact_name="tipo_renta", reason="falta", priority=RiskLevel.HIGH)],
        created_at=now,
        updated_at=now,
    )

    interpretation = InterpretationResult(
        intent=Intent.DECLARACION_IRPF,
        confidence=0.8,
        extracted_facts=[
            Fact(
                fact_id="f1",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="user_input",
                confidence=0.9,
            )
        ],
        uncertainties=[
            Uncertainty(
                code="u1",
                message="Falta confirmar tipo de renta.",
                severity=RiskLevel.HIGH,
            )
        ],
        missing_facts=[MissingFact(fact_name="tipo_renta", reason="falta", priority=RiskLevel.HIGH)],
        next_questions=[
            QuestionPrompt(
                question_id="q1",
                question_text="¿Qué tipo de ingresos has tenido?",
                target_fact="tipo_renta",
                reason="bloqueante",
                priority=RiskLevel.HIGH,
            )
        ],
    )

    obligation = ObligationCandidate(
        obligation_id="obl_irpf",
        title="Posible IRPF",
        description="desc",
        jurisdiction="ES",
        tax_period="2025",
        risk_level=RiskLevel.HIGH,
        confidence=0.86,
        trigger_facts=["residencia_fiscal"],
        blocking_missing_facts=["tipo_renta"],
        evidence_refs=[
            EvidenceRef(
                evidence_id="ev1",
                source_type=EvidenceSourceType.RETRIEVED_DOCUMENT,
                title="Norma IRPF",
                locator="https://example.org/irpf",
                confidence=0.9,
            )
        ],
        created_at=now,
        updated_at=now,
    )

    action = ActionItem(
        action_id="a1",
        title="Confirmar ingresos",
        description="desc",
        priority=1,
        risk_level=RiskLevel.HIGH,
        expected_outcome="ok",
        confidence=0.8,
    )

    planner = PlannerResult(
        ranked_actions=[
            PlannedAction(
                action=action,
                score=0.9,
                score_breakdown={"sanction_risk": 1.0, "urgency": 0.8, "user_effort": 0.5, "compliance_impact": 0.9},
                checklist_documental=["Certificado de retenciones"],
                bloqueos=["tipo_renta"],
            )
        ],
        next_best_action=PlannedAction(
            action=action,
            score=0.9,
            score_breakdown={"sanction_risk": 1.0, "urgency": 0.8, "user_effort": 0.5, "compliance_impact": 0.9},
            checklist_documental=["Certificado de retenciones"],
            bloqueos=["tipo_renta"],
        ),
        weights={"sanction_risk": 0.35, "urgency": 0.25, "user_effort": 0.15, "compliance_impact": 0.25},
    )

    text = ExplanationComposer().compose(case, interpretation, [obligation], planner)

    assert "1) Hechos detectados" in text
    assert "2) Incertidumbres" in text
    assert "3) Obligaciones candidatas + confianza" in text
    assert "4) Plan recomendado" in text
    assert "5) Fuentes" in text
    assert "6) Próxima pregunta óptima" in text
    assert "Norma IRPF" in text
    assert "¿Qué tipo de ingresos has tenido?" in text
