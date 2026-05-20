from datetime import UTC, datetime

from hacienda_gpt.decision.rules import DecisionRule, RuleSet
from hacienda_gpt.decision.rules_engine import RulesEngine
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType


def _fact(name: str, value) -> Fact:
    value_type = FactValueType.BOOLEAN if isinstance(value, bool) else FactValueType.STRING
    return Fact(fact_id=f"f_{name}", name=name, value=value, value_type=value_type, source="test", confidence=1.0)


def _case(period: str) -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="case_t",
        user_id="u",
        jurisdiction="ES",
        tax_period=period,
        facts=[_fact("residencia_fiscal", "ES"), _fact("menciona_ingresos", True)],
        created_at=now,
        updated_at=now,
    )


def test_selects_only_rules_applicable_to_fiscal_year() -> None:
    ruleset = RuleSet.model_validate(
        {
            "rules": [
                {
                    "id": "r_old",
                    "jurisdiction": "ES",
                    "valid_from": "2020-01-01",
                    "valid_to": "2023-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {"obligation_id": "obl_a", "title": "A", "description": "A", "status": "candidate"},
                    "base_confidence": 0.6,
                    "risk_level": "low",
                },
                {
                    "id": "r_new",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {"obligation_id": "obl_b", "title": "B", "description": "B", "status": "candidate"},
                    "base_confidence": 0.7,
                    "risk_level": "medium",
                },
            ]
        }
    )
    engine = RulesEngine(ruleset)
    result = engine.evaluate(_case("2025"), recent_facts=[])
    assert len(result.rule_traces) == 1
    assert result.rule_traces[0].rule_id == "r_new"


def test_conflict_resolution_keeps_highest_confidence_for_same_obligation() -> None:
    ruleset = RuleSet.model_validate(
        {
            "rules": [
                {
                    "id": "r1",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {"obligation_id": "obl_same", "title": "Same", "description": "D", "status": "candidate"},
                    "base_confidence": 0.6,
                    "risk_level": "medium",
                },
                {
                    "id": "r2",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {"obligation_id": "obl_same", "title": "Same2", "description": "D2", "status": "candidate"},
                    "base_confidence": 0.9,
                    "risk_level": "high",
                },
            ]
        }
    )
    engine = RulesEngine(ruleset)
    result = engine.evaluate(_case("2025"), recent_facts=[])
    assert len(result.candidate_obligations) == 1
    assert result.candidate_obligations[0].confidence == 0.9
    assert any(t.conflict_resolved for t in result.rule_traces if t.matched)


def test_records_exact_rule_and_ruleset_versions_for_auditability() -> None:
    ruleset = RuleSet.model_validate(
        {
            "rules": [
                {
                    "id": "r_ver",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {"obligation_id": "obl_v", "title": "V", "description": "V", "status": "candidate"},
                    "base_confidence": 0.8,
                    "risk_level": "medium",
                }
            ]
        }
    )
    engine = RulesEngine(ruleset)
    result = engine.evaluate(_case("2025"), recent_facts=[])
    assert result.ruleset_version != ""
    assert result.rule_traces[0].rule_version != ""
    assert len(result.rule_traces[0].rule_version) == 64
