from pathlib import Path

import pytest
from pydantic import ValidationError

from hacienda_gpt.decision.rules import DecisionRule, RuleSet, load_rules_from_directory, load_rules_from_json


def test_load_rules_from_json_file() -> None:
    ruleset = load_rules_from_json("rules/irpf_candidate_rules.json")
    assert len(ruleset.rules) >= 2
    assert ruleset.rules[0].jurisdiction == "ES"


def test_load_rules_from_directory() -> None:
    ruleset = load_rules_from_directory("rules")
    assert len(ruleset.rules) >= 2


def test_rule_rejects_invalid_date_range() -> None:
    with pytest.raises(ValidationError):
        DecisionRule.model_validate(
            {
                "id": "r1",
                "jurisdiction": "ES",
                "valid_from": "2026-01-01",
                "valid_to": "2025-01-01",
                "conditions": [{"fact": "residencia_fiscal", "operator": "exists"}],
                "required_facts": ["residencia_fiscal"],
                "generated_obligation": {
                    "obligation_id": "obl1",
                    "title": "t",
                    "description": "d",
                    "status": "candidate",
                },
                "base_confidence": 0.5,
                "risk_level": "medium",
            }
        )


def test_ruleset_rejects_duplicate_rule_ids() -> None:
    with pytest.raises(ValidationError):
        RuleSet.model_validate(
            {
                "rules": [
                    {
                        "id": "dup",
                        "jurisdiction": "ES",
                        "valid_from": "2024-01-01",
                        "valid_to": "2024-12-31",
                        "conditions": [{"fact": "x", "operator": "exists"}],
                        "required_facts": ["x"],
                        "generated_obligation": {
                            "obligation_id": "obl1",
                            "title": "t",
                            "description": "d",
                            "status": "candidate",
                        },
                        "base_confidence": 0.4,
                        "risk_level": "low",
                    },
                    {
                        "id": "dup",
                        "jurisdiction": "ES",
                        "valid_from": "2024-01-01",
                        "valid_to": "2024-12-31",
                        "conditions": [{"fact": "y", "operator": "exists"}],
                        "required_facts": ["y"],
                        "generated_obligation": {
                            "obligation_id": "obl2",
                            "title": "t2",
                            "description": "d2",
                            "status": "candidate",
                        },
                        "base_confidence": 0.4,
                        "risk_level": "low",
                    },
                ]
            }
        )


def test_invalid_json_format_raises(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"foo": "bar"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules_from_json(invalid)
