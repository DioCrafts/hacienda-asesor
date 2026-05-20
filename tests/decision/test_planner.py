from datetime import UTC, datetime

from hacienda_gpt.decision.planner import Planner, PlannerWeights
from hacienda_gpt.decision.schemas import CaseState, ObligationCandidate, RiskLevel


def _case() -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="c1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        facts=[],
        created_at=now,
        updated_at=now,
    )


def _obligation(obligation_id: str, risk: RiskLevel, confidence: float, missing: list[str], tax_period: str = "2025") -> ObligationCandidate:
    now = datetime.now(UTC)
    return ObligationCandidate(
        obligation_id=obligation_id,
        title=obligation_id,
        description="desc",
        jurisdiction="ES",
        tax_period=tax_period,
        risk_level=risk,
        confidence=confidence,
        trigger_facts=["residencia_fiscal"],
        blocking_missing_facts=missing,
        created_at=now,
        updated_at=now,
    )


def test_planner_ranks_higher_risk_and_confidence_first() -> None:
    planner = Planner(PlannerWeights(0.35, 0.25, 0.15, 0.25))
    result = planner.plan(
        case_state=_case(),
        obligations=[
            _obligation("obl_low", RiskLevel.LOW, 0.4, ["x", "y"]),
            _obligation("obl_high", RiskLevel.HIGH, 0.9, []),
        ],
    )

    assert result.ranked_actions[0].action.action_id == "action_obl_high"
    assert result.ranked_actions[0].action.priority == 1


def test_planner_is_sensitive_to_weight_changes() -> None:
    high_effort_first = Planner(PlannerWeights(0.1, 0.1, 0.7, 0.1))
    high_risk_first = Planner(PlannerWeights(0.7, 0.1, 0.1, 0.1))

    obligations = [
        _obligation("obl_easy_medium", RiskLevel.MEDIUM, 0.8, []),
        _obligation("obl_hard_critical", RiskLevel.CRITICAL, 0.9, ["a", "b", "c"]),
    ]

    result_effort = high_effort_first.plan(_case(), obligations)
    result_risk = high_risk_first.plan(_case(), obligations)

    assert result_effort.ranked_actions[0].action.action_id == "action_obl_easy_medium"
    assert result_risk.ranked_actions[0].action.action_id == "action_obl_hard_critical"
