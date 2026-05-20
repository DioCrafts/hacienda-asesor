from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.schemas import ActionItem, CaseState, ObligationCandidate, RiskLevel


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    return float(value)


@dataclass(frozen=True)
class PlannerWeights:
    sanction_risk: float
    urgency: float
    user_effort: float
    compliance_impact: float

    @classmethod
    def from_env(cls) -> "PlannerWeights":
        return cls(
            sanction_risk=_env_float("PLANNER_WEIGHT_SANCTION_RISK", 0.35),
            urgency=_env_float("PLANNER_WEIGHT_URGENCY", 0.25),
            user_effort=_env_float("PLANNER_WEIGHT_USER_EFFORT", 0.15),
            compliance_impact=_env_float("PLANNER_WEIGHT_COMPLIANCE_IMPACT", 0.25),
        )


class PlannedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionItem
    score: float = Field(ge=0.0)
    score_breakdown: dict[str, float]
    checklist_documental: list[str] = Field(default_factory=list)
    bloqueos: list[str] = Field(default_factory=list)


class PlannerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ranked_actions: list[PlannedAction]
    next_best_action: PlannedAction | None = None
    weights: dict[str, float]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Planner:
    def __init__(self, weights: PlannerWeights | None = None) -> None:
        self.weights = weights or PlannerWeights.from_env()

    def plan(self, case_state: CaseState, obligations: list[ObligationCandidate]) -> PlannerResult:
        del case_state
        actions: list[PlannedAction] = []

        for obligation in obligations:
            urgency = self._urgency_score(obligation.tax_period)
            sanction = self._sanction_risk_score(obligation.risk_level)
            effort = self._user_effort_score(obligation)
            impact = self._compliance_impact_score(obligation)

            score = (
                self.weights.sanction_risk * sanction
                + self.weights.urgency * urgency
                + self.weights.user_effort * effort
                + self.weights.compliance_impact * impact
            )

            action = ActionItem(
                action_id=f"action_{obligation.obligation_id}",
                title=f"Revisar obligación: {obligation.title}",
                description=obligation.description,
                priority=1,
                risk_level=obligation.risk_level,
                due_date=date(int(obligation.tax_period), 12, 31),
                depends_on=[],
                expected_outcome=f"Confirmar o descartar {obligation.obligation_id}",
                confidence=obligation.confidence,
            )

            checklist = self._build_document_checklist(obligation)
            bloqueos = list(obligation.blocking_missing_facts)

            actions.append(
                PlannedAction(
                    action=action,
                    score=round(score, 4),
                    score_breakdown={
                        "sanction_risk": round(sanction, 4),
                        "urgency": round(urgency, 4),
                        "user_effort": round(effort, 4),
                        "compliance_impact": round(impact, 4),
                    },
                    checklist_documental=checklist,
                    bloqueos=bloqueos,
                )
            )

        ranked = sorted(actions, key=lambda item: item.score, reverse=True)

        # Dependency strategy: each subsequent action depends on previous one.
        for index, item in enumerate(ranked, start=1):
            item.action.priority = index
            if index > 1:
                item.action.depends_on = [ranked[index - 2].action.action_id]

        next_best = ranked[0] if ranked else None

        return PlannerResult(
            ranked_actions=ranked,
            next_best_action=next_best,
            weights={
                "sanction_risk": self.weights.sanction_risk,
                "urgency": self.weights.urgency,
                "user_effort": self.weights.user_effort,
                "compliance_impact": self.weights.compliance_impact,
            },
        )

    def _build_document_checklist(self, obligation: ObligationCandidate) -> list[str]:
        checklist: list[str] = []
        for evidence in obligation.evidence_refs:
            label = evidence.title.strip()
            locator = evidence.locator.strip()
            checklist.append(f"{label} ({locator})")
        if not checklist:
            checklist.append("Aportar documentación de soporte del hecho fiscal declarado")
        return checklist

    def _sanction_risk_score(self, risk_level: RiskLevel) -> float:
        return {
            RiskLevel.LOW: 0.25,
            RiskLevel.MEDIUM: 0.5,
            RiskLevel.HIGH: 0.8,
            RiskLevel.CRITICAL: 1.0,
        }[risk_level]

    def _urgency_score(self, tax_period: str) -> float:
        now_year = datetime.now(UTC).year
        delta = max(0, now_year - int(tax_period))
        return min(1.0, 0.4 + delta * 0.2)

    def _user_effort_score(self, obligation: ObligationCandidate) -> float:
        # Higher score means lower user effort (preferred).
        missing = len(obligation.blocking_missing_facts)
        return max(0.1, 1.0 - 0.2 * missing)

    def _compliance_impact_score(self, obligation: ObligationCandidate) -> float:
        return min(1.0, 0.5 + 0.5 * obligation.confidence)
