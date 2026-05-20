from datetime import UTC, datetime

from hacienda_gpt.decision.rules import DecisionRule, RuleCondition, RuleSet
from hacienda_gpt.decision.rules_engine import RulesEngine
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, ObligationStatus, RiskLevel


def _case_with_facts(*facts: Fact) -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="case1",
        user_id="user1",
        jurisdiction="ES",
        tax_period="2025",
        facts=list(facts),
        created_at=now,
        updated_at=now,
    )


def _fact(name: str, value) -> Fact:
    value_type = FactValueType.BOOLEAN if isinstance(value, bool) else FactValueType.STRING
    return Fact(
        fact_id=f"fact_{name}",
        name=name,
        value=value,
        value_type=value_type,
        source="test",
        confidence=1.0,
    )


def test_rules_engine_emits_candidate_and_trace_when_rule_matches() -> None:
    ruleset = RuleSet(
        rules=[
            DecisionRule.model_validate(
                {
                    "id": "r1",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [
                        {"fact": "residencia_fiscal", "operator": "eq", "value": "ES"},
                        {"fact": "menciona_ingresos", "operator": "eq", "value": True},
                    ],
                    "required_facts": ["residencia_fiscal", "menciona_ingresos"],
                    "generated_obligation": {
                        "obligation_id": "obl_irpf",
                        "title": "Posible IRPF",
                        "description": "desc",
                        "status": "candidate",
                    },
                    "base_confidence": 0.8,
                    "risk_level": "medium",
                }
            )
        ]
    )

    engine = RulesEngine(ruleset)
    case = _case_with_facts(_fact("residencia_fiscal", "ES"))
    result = engine.evaluate(case_state=case, recent_facts=[_fact("menciona_ingresos", True)])

    assert len(result.candidate_obligations) == 1
    assert result.candidate_obligations[0].obligation_id == "obl_irpf"
    assert result.candidate_obligations[0].status is ObligationStatus.CANDIDATE
    assert len(result.rule_traces) == 1
    assert result.rule_traces[0].matched is True
    assert result.rule_traces[0].missing_facts == []
    assert len(result.rule_traces[0].activation_reasons) == 2


def test_rules_engine_reports_missing_facts_and_no_candidate() -> None:
    ruleset = RuleSet(
        rules=[
            DecisionRule.model_validate(
                {
                    "id": "r2",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [
                        {"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}
                    ],
                    "required_facts": ["residencia_fiscal", "tipo_renta"],
                    "generated_obligation": {
                        "obligation_id": "obl_x",
                        "title": "Posible X",
                        "description": "desc",
                        "status": "candidate",
                    },
                    "base_confidence": 0.7,
                    "risk_level": "high",
                }
            )
        ]
    )

    engine = RulesEngine(ruleset)
    case = _case_with_facts(_fact("residencia_fiscal", "ES"))
    result = engine.evaluate(case_state=case, recent_facts=[])

    assert result.candidate_obligations == []
    assert result.rule_traces[0].matched is False
    assert "tipo_renta" in result.rule_traces[0].missing_facts


def test_rules_engine_integration_loads_real_rules_directory() -> None:
    engine = RulesEngine.from_rules_directory("rules")
    now = datetime.now(UTC)
    case = CaseState(
        case_id="case_real",
        user_id="u_real",
        jurisdiction="ES",
        tax_period="2025",
        facts=[
            Fact(
                fact_id="f1",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="test",
                confidence=1.0,
            )
        ],
        created_at=now,
        updated_at=now,
    )
    recent = [
        Fact(
            fact_id="f2",
            name="menciona_ingresos",
            value=True,
            value_type=FactValueType.BOOLEAN,
            source="test",
            confidence=1.0,
        )
    ]

    result = engine.evaluate(case_state=case, recent_facts=recent)
    assert len(result.rule_traces) >= 1
    assert any(trace.rule_id for trace in result.rule_traces)
