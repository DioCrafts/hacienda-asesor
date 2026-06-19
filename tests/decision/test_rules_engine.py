from datetime import UTC, datetime

from hacienda_gpt.decision.rules import DecisionRule, RuleSet
from hacienda_gpt.decision.rules_engine import (
    RulesEngine,
    _engine_for_directory,
    clear_rules_cache,
    evaluate_rules,
)
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType, ObligationStatus


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


def test_matched_obligation_carries_rule_source_refs_as_evidence() -> None:
    # A matched rule's normative source_refs must surface on the obligation's
    # evidence_refs so the audit trail / explainer can cite the backing norm.
    zero_hash = "0" * 64
    ruleset = RuleSet(
        rules=[
            DecisionRule.model_validate(
                {
                    "id": "r_evidence",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
                    "required_facts": ["residencia_fiscal"],
                    "generated_obligation": {
                        "obligation_id": "obl_ev",
                        "title": "Posible IRPF",
                        "description": "desc",
                        "status": "candidate",
                    },
                    "base_confidence": 0.8,
                    "risk_level": "medium",
                    "source_refs": [
                        {
                            "source_id": "BOE-A-2006-20764",
                            "locator": "boe://BOE-A-2006-20764",
                            "content_hash": zero_hash,
                            "last_reviewed_at": "2024-12-01",
                            "notes": "Ley 35/2006 IRPF",
                        }
                    ],
                }
            )
        ]
    )

    engine = RulesEngine(ruleset)
    case = _case_with_facts(_fact("residencia_fiscal", "ES"))
    result = engine.evaluate(case_state=case, recent_facts=[])

    obligation = result.candidate_obligations[0]
    assert len(obligation.evidence_refs) == 1
    evidence = obligation.evidence_refs[0]
    assert evidence.evidence_id == "BOE-A-2006-20764"
    assert evidence.locator == "boe://BOE-A-2006-20764"
    assert evidence.title == "Ley 35/2006 IRPF"
    assert evidence.hash == zero_hash


def test_rules_engine_reports_missing_facts_and_no_candidate() -> None:
    ruleset = RuleSet(
        rules=[
            DecisionRule.model_validate(
                {
                    "id": "r2",
                    "jurisdiction": "ES",
                    "valid_from": "2024-01-01",
                    "valid_to": "2026-12-31",
                    "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
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


def test_evaluate_rules_reuses_cached_engine_per_directory() -> None:
    clear_rules_cache()
    first = _engine_for_directory("rules")
    second = _engine_for_directory("rules")
    assert first is second  # same warm engine, not re-parsed from disk

    case = _case_with_facts(_fact("residencia_fiscal", "ES"))
    recent = [_fact("menciona_ingresos", True)]
    # Two turns through the cached path produce identical obligation ids.
    r1 = evaluate_rules(case_state=case, recent_facts=recent)
    r2 = evaluate_rules(case_state=case, recent_facts=recent)
    assert [o.obligation_id for o in r1.candidate_obligations] == [o.obligation_id for o in r2.candidate_obligations]

    clear_rules_cache()
    assert _engine_for_directory("rules") is not first  # cache cleared -> fresh build


def test_evaluate_rules_loads_rules_from_any_cwd(tmp_path, monkeypatch) -> None:
    # Regression: the rules directory must resolve to an absolute path so the
    # engine loads rules regardless of the process working directory (uvicorn /
    # Streamlit may start elsewhere). Before the fix the relative "rules" default
    # found nothing from another CWD and RuleSet(min_length=1) raised, stalling
    # every turn with a 500 instead of detecting obligations.
    clear_rules_cache()
    monkeypatch.chdir(tmp_path)  # a CWD that is NOT the repo root
    case = _case_with_facts(_fact("residencia_fiscal", "ES"))
    recent = [_fact("menciona_ingresos", True)]
    result = evaluate_rules(case_state=case, recent_facts=recent)
    assert result.rule_traces, "rules must load from an absolute path, not the CWD"
    clear_rules_cache()


def test_rule_version_is_memoized() -> None:
    rule = DecisionRule.model_validate(
        {
            "id": "r_mem",
            "jurisdiction": "ES",
            "valid_from": "2024-01-01",
            "valid_to": "2026-12-31",
            "conditions": [{"fact": "residencia_fiscal", "operator": "eq", "value": "ES"}],
            "required_facts": ["residencia_fiscal"],
            "generated_obligation": {
                "obligation_id": "obl_mem",
                "title": "t",
                "description": "d",
                "status": "candidate",
            },
            "base_confidence": 0.5,
            "risk_level": "low",
        }
    )
    engine = RulesEngine(RuleSet(rules=[rule]))
    version = engine._rule_version(rule)
    assert engine._version_cache[rule.id] == version
    assert engine._rule_version(rule) == version  # second call hits the cache
