from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.rules import ConditionOperator, DecisionRule, RuleCondition, RuleSet, load_rules_from_directory
from hacienda_gpt.decision.schemas import CaseState, Fact, ObligationCandidate


class ConditionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str
    operator: str
    expected_value: Any = None
    actual_value: Any = None
    matched: bool


class RuleTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    matched: bool
    activation_reasons: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    condition_traces: list[ConditionTrace] = Field(default_factory=list)
    rule_version: str
    rule_valid_from: date
    rule_valid_to: date
    conflict_resolved: bool = False
    conflict_strategy: str | None = None


class RulesEngineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_obligations: list[ObligationCandidate] = Field(default_factory=list)
    rule_traces: list[RuleTrace] = Field(default_factory=list)
    ruleset_version: str
    fiscal_year: int


@dataclass(frozen=True)
class RulesEngine:
    ruleset: RuleSet

    @classmethod
    def from_rules_directory(cls, directory: str = "rules") -> "RulesEngine":
        return cls(ruleset=load_rules_from_directory(directory))

    def evaluate(self, case_state: CaseState, recent_facts: list[Fact]) -> RulesEngineResult:
        fiscal_year = self._fiscal_year_from_case(case_state)
        applicable_rules = self._select_applicable_rules(fiscal_year)
        fact_map = self._build_fact_map(case_state, recent_facts)

        traces: list[RuleTrace] = []
        obligations: list[ObligationCandidate] = []

        for rule in applicable_rules:
            trace, matched = self._evaluate_rule(rule, fact_map)
            traces.append(trace)
            if matched:
                obligations.append(self._build_candidate_obligation(rule, case_state, trace.missing_facts))

        deduped_obligations, updated_traces = self._resolve_conflicts(obligations, traces)

        return RulesEngineResult(
            candidate_obligations=deduped_obligations,
            rule_traces=updated_traces,
            ruleset_version=self._ruleset_version(applicable_rules),
            fiscal_year=fiscal_year,
        )

    def _fiscal_year_from_case(self, case_state: CaseState) -> int:
        return int(case_state.tax_period)

    def _select_applicable_rules(self, fiscal_year: int) -> list[DecisionRule]:
        anchor = date(fiscal_year, 12, 31)
        return [rule for rule in self.ruleset.rules if rule.valid_from <= anchor <= rule.valid_to]

    def _build_fact_map(self, case_state: CaseState, recent_facts: list[Fact]) -> dict[str, Any]:
        merged = {fact.name: fact.value for fact in case_state.facts}
        for fact in recent_facts:
            merged[fact.name] = fact.value
        return merged

    def _evaluate_rule(self, rule: DecisionRule, fact_map: dict[str, Any]) -> tuple[RuleTrace, bool]:
        condition_traces: list[ConditionTrace] = []
        all_matched = True
        activation_reasons: list[str] = []

        for condition in rule.conditions:
            actual = fact_map.get(condition.fact)
            matched = self._matches_condition(condition, actual)
            if matched:
                activation_reasons.append(f"{condition.fact} {condition.operator.value} {condition.value}")
            else:
                all_matched = False
            condition_traces.append(
                ConditionTrace(
                    fact=condition.fact,
                    operator=condition.operator.value,
                    expected_value=condition.value,
                    actual_value=actual,
                    matched=matched,
                )
            )

        missing_facts = [fact for fact in rule.required_facts if fact not in fact_map]
        if missing_facts:
            all_matched = False

        trace = RuleTrace(
            rule_id=rule.id,
            matched=all_matched,
            activation_reasons=activation_reasons,
            missing_facts=missing_facts,
            condition_traces=condition_traces,
            rule_version=self._rule_version(rule),
            rule_valid_from=rule.valid_from,
            rule_valid_to=rule.valid_to,
        )
        return trace, all_matched

    def _matches_condition(self, condition: RuleCondition, actual_value: Any) -> bool:
        op = condition.operator
        expected = condition.value
        if op is ConditionOperator.EXISTS:
            return actual_value is not None
        if actual_value is None:
            return False
        if op is ConditionOperator.EQ:
            return actual_value == expected
        if op is ConditionOperator.NEQ:
            return actual_value != expected
        if op is ConditionOperator.IN:
            return isinstance(expected, list) and actual_value in expected
        if op is ConditionOperator.GTE:
            return float(actual_value) >= float(expected)
        if op is ConditionOperator.LTE:
            return float(actual_value) <= float(expected)
        return False

    def _build_candidate_obligation(self, rule: DecisionRule, case_state: CaseState, missing_facts: list[str]) -> ObligationCandidate:
        now = datetime.now(UTC)
        return ObligationCandidate(
            obligation_id=rule.generated_obligation.obligation_id,
            title=rule.generated_obligation.title,
            description=rule.generated_obligation.description,
            jurisdiction=rule.jurisdiction,
            tax_period=case_state.tax_period,
            status=rule.generated_obligation.status,
            risk_level=rule.risk_level,
            confidence=rule.base_confidence,
            trigger_facts=[cond.fact for cond in rule.conditions],
            blocking_missing_facts=missing_facts,
            evidence_refs=[],
            created_at=now,
            updated_at=now,
        )

    def _resolve_conflicts(
        self,
        obligations: list[ObligationCandidate],
        traces: list[RuleTrace],
    ) -> tuple[list[ObligationCandidate], list[RuleTrace]]:
        # Conflict strategy: for same obligation_id keep highest confidence.
        by_id: dict[str, ObligationCandidate] = {}
        for obligation in obligations:
            existing = by_id.get(obligation.obligation_id)
            if existing is None or obligation.confidence > existing.confidence:
                by_id[obligation.obligation_id] = obligation

        kept_ids = set(by_id.keys())
        matched_by_obligation = {ob.obligation_id for ob in obligations}
        conflict_happened = len(obligations) != len(kept_ids)

        if conflict_happened:
            for trace in traces:
                if trace.matched:
                    trace.conflict_resolved = True
                    trace.conflict_strategy = "highest_confidence_per_obligation_id"

        return list(by_id.values()), traces

    def _rule_version(self, rule: DecisionRule) -> str:
        payload = rule.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _ruleset_version(self, rules: list[DecisionRule]) -> str:
        if not rules:
            return "empty"
        versions = sorted(self._rule_version(rule) for rule in rules)
        return hashlib.sha256("|".join(versions).encode("utf-8")).hexdigest()


def evaluate_rules(case_state: CaseState, recent_facts: list[Fact], rules_directory: str = "rules") -> RulesEngineResult:
    engine = RulesEngine.from_rules_directory(rules_directory)
    return engine.evaluate(case_state=case_state, recent_facts=recent_facts)
