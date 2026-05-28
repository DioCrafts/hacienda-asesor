from pathlib import Path

import pytest
from pydantic import ValidationError

from hacienda_gpt.decision.rules import (
    DecisionRule,
    RuleSet,
    RuleSourceRef,
    load_rules_from_directory,
    load_rules_from_json,
)


_VALID_HASH = "a" * 64


def _minimal_rule_payload(**overrides: object) -> dict:
    payload: dict = {
        "id": "r1",
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
    }
    payload.update(overrides)
    return payload


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


def test_decision_rule_defaults_source_refs_to_empty_list() -> None:
    rule = DecisionRule.model_validate(_minimal_rule_payload())
    assert rule.source_refs == []
    assert rule.last_reviewed_at is None


def test_decision_rule_accepts_source_refs() -> None:
    payload = _minimal_rule_payload(
        last_reviewed_at="2024-12-01",
        source_refs=[
            {
                "source_id": "BOE-X",
                "locator": "boe://BOE-X",
                "content_hash": _VALID_HASH,
                "last_reviewed_at": "2024-12-01",
                "notes": "art. 1",
            }
        ],
    )
    rule = DecisionRule.model_validate(payload)
    assert len(rule.source_refs) == 1
    assert rule.source_refs[0].source_id == "BOE-X"


def test_source_ref_rejects_invalid_hash() -> None:
    with pytest.raises(ValidationError):
        RuleSourceRef.model_validate(
            {
                "source_id": "BOE-X",
                "locator": "boe://BOE-X",
                "content_hash": "not-a-hash" * 6 + "abcd",
                "last_reviewed_at": "2024-12-01",
            }
        )


def test_decision_rule_rejects_duplicate_source_ids() -> None:
    payload = _minimal_rule_payload(
        source_refs=[
            {
                "source_id": "dup",
                "locator": "boe://A",
                "content_hash": _VALID_HASH,
                "last_reviewed_at": "2024-12-01",
            },
            {
                "source_id": "dup",
                "locator": "boe://B",
                "content_hash": _VALID_HASH,
                "last_reviewed_at": "2024-12-01",
            },
        ],
    )
    with pytest.raises(ValidationError):
        DecisionRule.model_validate(payload)


def test_irpf_seed_rules_include_source_refs() -> None:
    ruleset = load_rules_from_json("rules/irpf_candidate_rules.json")
    for rule in ruleset.rules:
        assert rule.source_refs, f"rule {rule.id} should declare normative source_refs"
        assert rule.last_reviewed_at is not None
