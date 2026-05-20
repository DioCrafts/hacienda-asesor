from datetime import UTC, datetime

from hacienda_gpt.decision.audit import build_recommendation_audit_event
from hacienda_gpt.decision.interpreter import Intent, InterpretationResult
from hacienda_gpt.decision.rules_engine import RulesEngineResult, RuleTrace
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType


def test_audit_event_contains_required_trace_fields() -> None:
    now = datetime.now(UTC)
    case = CaseState(case_id="c1", user_id="u1", jurisdiction="ES", tax_period="2025", created_at=now, updated_at=now)
    interpretation = InterpretationResult(
        intent=Intent.DECLARACION_IRPF,
        confidence=0.8,
        extracted_facts=[
            Fact(fact_id="f1", name="residencia_fiscal", value="ES", value_type=FactValueType.STRING, source="user", confidence=0.9)
        ],
    )
    rules_result = RulesEngineResult(
        candidate_obligations=[],
        rule_traces=[
            RuleTrace(
                rule_id="r1",
                matched=True,
                activation_reasons=["x"],
                missing_facts=[],
                condition_traces=[],
                rule_version="abc",
                rule_valid_from=now.date(),
                rule_valid_to=now.date(),
            )
        ],
        ruleset_version="rs1",
        fiscal_year=2025,
    )

    event = build_recommendation_audit_event(case, interpretation, rules_result, [])
    assert event["event_type"] == "recommendation_audit"
    assert "facts_used" in event
    assert "rules_triggered" in event
    assert "evidences_cited" in event
    assert "versions" in event
