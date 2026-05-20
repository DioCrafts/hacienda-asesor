from datetime import UTC, datetime

from hacienda_gpt.decision.planner import Planner, PlannerWeights
from hacienda_gpt.decision.schemas import CaseState, EvidenceRef, EvidenceSourceType, ObligationCandidate, RiskLevel


def _case() -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="case_e2e",
        user_id="u_e2e",
        jurisdiction="ES",
        tax_period="2025",
        facts=[],
        created_at=now,
        updated_at=now,
    )


def _obligation(oid: str, risk: RiskLevel, confidence: float, missing: list[str], with_evidence: bool) -> ObligationCandidate:
    now = datetime.now(UTC)
    evidence = []
    if with_evidence:
        evidence = [
            EvidenceRef(
                evidence_id=f"ev_{oid}",
                source_type=EvidenceSourceType.RETRIEVED_DOCUMENT,
                title=f"Norma {oid}",
                locator="https://example.org/norma",
                confidence=0.9,
            )
        ]
    return ObligationCandidate(
        obligation_id=oid,
        title=f"Obligación {oid}",
        description="Revisión obligación",
        jurisdiction="ES",
        tax_period="2025",
        risk_level=risk,
        confidence=confidence,
        trigger_facts=["residencia_fiscal"],
        blocking_missing_facts=missing,
        evidence_refs=evidence,
        created_at=now,
        updated_at=now,
    )


def test_planner_generates_prioritized_action_plan_with_next_best_and_dependencies() -> None:
    planner = Planner(PlannerWeights(0.5, 0.2, 0.1, 0.2))
    obligations = [
        _obligation("obl_a", RiskLevel.HIGH, 0.9, ["tipo_renta"], with_evidence=True),
        _obligation("obl_b", RiskLevel.MEDIUM, 0.7, ["importe_renta", "periodicidad"], with_evidence=False),
    ]

    result = planner.plan(_case(), obligations)

    assert len(result.ranked_actions) == 2
    assert result.next_best_action is not None
    assert result.next_best_action.action.action_id == result.ranked_actions[0].action.action_id

    first = result.ranked_actions[0]
    second = result.ranked_actions[1]

    assert first.action.priority == 1
    assert second.action.priority == 2
    assert second.action.depends_on == [first.action.action_id]
    assert first.action.due_date is not None

    assert first.checklist_documental
    assert first.bloqueos == ["tipo_renta"]
    assert second.bloqueos == ["importe_renta", "periodicidad"]


def test_planner_weight_sensitivity_changes_next_best_action() -> None:
    easy = _obligation("easy", RiskLevel.MEDIUM, 0.7, [], with_evidence=False)
    critical = _obligation("critical", RiskLevel.CRITICAL, 0.8, ["a", "b", "c"], with_evidence=True)

    effort_weighted = Planner(PlannerWeights(0.1, 0.1, 0.7, 0.1)).plan(_case(), [easy, critical])
    risk_weighted = Planner(PlannerWeights(0.7, 0.1, 0.1, 0.1)).plan(_case(), [easy, critical])

    assert effort_weighted.next_best_action is not None
    assert risk_weighted.next_best_action is not None
    assert effort_weighted.next_best_action.action.action_id == "action_easy"
    assert risk_weighted.next_best_action.action.action_id == "action_critical"
