from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hacienda_gpt.decision.schemas import (
    SCHEMA_VERSION,
    ActionItem,
    CaseState,
    CaseStatus,
    DecisionOutput,
    EvidenceRef,
    EvidenceSourceType,
    Fact,
    FactValueType,
    MissingFact,
    ObligationCandidate,
    ObligationStatus,
    RiskLevel,
)


def build_fact(**overrides: object) -> Fact:
    base = {
        "fact_id": "fact_1",
        "name": "residencia_fiscal",
        "value": "ES",
        "value_type": FactValueType.STRING,
        "source": "user_input",
        "confidence": 0.9,
    }
    return Fact(**(base | overrides))


def build_evidence(**overrides: object) -> EvidenceRef:
    base = {
        "evidence_id": "ev_1",
        "source_type": EvidenceSourceType.RETRIEVED_DOCUMENT,
        "title": "Fuente AEAT",
        "locator": "https://example.org",
        "confidence": 0.8,
    }
    return EvidenceRef(**(base | overrides))


def build_obligation(**overrides: object) -> ObligationCandidate:
    base = {
        "obligation_id": "obl_1",
        "title": "Posible obligación",
        "description": "Descripción",
        "jurisdiction": "ES",
        "tax_period": "2025",
        "risk_level": RiskLevel.MEDIUM,
        "confidence": 0.7,
        "trigger_facts": ["residencia_fiscal"],
    }
    return ObligationCandidate(**(base | overrides))


def test_fact_accepts_valid_data_and_defaults_timestamp() -> None:
    fact = build_fact()
    assert fact.value_type is FactValueType.STRING
    assert fact.confidence == 0.9
    assert isinstance(fact.updated_at, datetime)


def test_fact_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        build_fact(confidence=1.2)


def test_evidence_ref_has_default_schema_version() -> None:
    evidence = build_evidence()
    assert evidence.schema_version == SCHEMA_VERSION


def test_obligation_candidate_rejects_empty_trigger_fact() -> None:
    with pytest.raises(ValidationError):
        build_obligation(trigger_facts=[""])


def test_action_item_rejects_self_dependency() -> None:
    with pytest.raises(ValidationError):
        ActionItem(
            action_id="a1",
            title="Paso",
            description="Detalle",
            priority=1,
            risk_level=RiskLevel.HIGH,
            depends_on=["a1"],
            expected_outcome="resultado",
            confidence=0.9,
        )


def test_case_state_validates_unique_fact_ids() -> None:
    fact1 = build_fact(fact_id="fact_same", name="residencia")
    fact2 = build_fact(fact_id="fact_same", name="tipo_renta")
    with pytest.raises(ValidationError):
        CaseState(
            case_id="case_1",
            user_id="user_1",
            jurisdiction="ES",
            tax_period="2025",
            facts=[fact1, fact2],
        )


def test_case_state_rejects_overlap_between_known_and_missing_facts() -> None:
    with pytest.raises(ValidationError):
        CaseState(
            case_id="case_1",
            user_id="user_1",
            jurisdiction="ES",
            tax_period="2025",
            facts=[build_fact(name="tipo_renta")],
            missing_facts=[
                MissingFact(fact_name="tipo_renta", reason="falta", priority=RiskLevel.HIGH),
            ],
        )


def test_case_state_rejects_invalid_timestamp_order() -> None:
    with pytest.raises(ValidationError):
        CaseState(
            case_id="case_1",
            user_id="user_1",
            jurisdiction="ES",
            tax_period="2025",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_case_state_serializes_with_default_status_and_schema_version() -> None:
    case = CaseState(case_id="case_1", user_id="user_1", jurisdiction="ES", tax_period="2025")
    assert case.status is CaseStatus.OPEN
    assert case.schema_version == SCHEMA_VERSION


def test_decision_output_rejects_duplicate_action_ids() -> None:
    action_a = ActionItem(
        action_id="a1",
        title="Paso 1",
        description="Detalle",
        priority=1,
        risk_level=RiskLevel.HIGH,
        expected_outcome="ok",
        confidence=0.9,
    )
    action_b = ActionItem(
        action_id="a1",
        title="Paso 2",
        description="Detalle",
        priority=2,
        risk_level=RiskLevel.MEDIUM,
        expected_outcome="ok",
        confidence=0.8,
    )
    with pytest.raises(ValidationError):
        DecisionOutput(case_id="case_1", summary="Resumen", confidence=0.8, action_plan=[action_a, action_b])


def test_decision_output_rejects_unknown_dependency() -> None:
    action = ActionItem(
        action_id="a1",
        title="Paso",
        description="Detalle",
        priority=1,
        risk_level=RiskLevel.LOW,
        depends_on=["missing"],
        expected_outcome="ok",
        confidence=0.8,
    )
    with pytest.raises(ValidationError):
        DecisionOutput(case_id="case_1", summary="Resumen", confidence=0.8, action_plan=[action])


def test_decision_output_accepts_valid_graph() -> None:
    evidence = build_evidence()
    obligation = build_obligation(status=ObligationStatus.LIKELY, evidence_refs=[evidence])
    action_1 = ActionItem(
        action_id="a1",
        title="Recolectar datos",
        description="Recolectar certificados",
        priority=1,
        risk_level=RiskLevel.HIGH,
        expected_outcome="datos completos",
        confidence=0.9,
    )
    action_2 = ActionItem(
        action_id="a2",
        title="Reevaluar",
        description="Aplicar reglas con datos completos",
        priority=2,
        risk_level=RiskLevel.MEDIUM,
        depends_on=["a1"],
        expected_outcome="obligación confirmada o descartada",
        confidence=0.85,
    )
    output = DecisionOutput(
        case_id="case_1",
        summary="Plan inicial",
        confidence=0.82,
        obligations=[obligation],
        action_plan=[action_1, action_2],
        missing_facts=[MissingFact(fact_name="importe", reason="falta", priority=RiskLevel.HIGH)],
        evidence_refs=[evidence],
    )
    assert output.schema_version == SCHEMA_VERSION
    assert len(output.action_plan) == 2
