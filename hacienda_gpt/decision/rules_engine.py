from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.rules import (
    ConditionOperator,
    DecisionRule,
    RuleCondition,
    RuleSet,
    RuleSourceRef,
    load_rules_from_directory,
)
from hacienda_gpt.decision.schemas import (
    CaseState,
    EvidenceRef,
    EvidenceSourceType,
    Fact,
    ObligationCandidate,
    parse_fiscal_year,
)
from hacienda_gpt.settings import RULES_DIR


def _source_ref_to_evidence(ref: RuleSourceRef, confidence: float) -> EvidenceRef:
    """Project a rule's normative ``RuleSourceRef`` onto an ``EvidenceRef``.

    The obligation-level evidence is what downstream consumers (audit trail,
    explainer, planner) cite, so it must carry the same locator + content hash
    the drift detector tracks. ``title`` falls back to the source id when the
    rule author left ``notes`` empty (the schema forbids an empty title).
    """
    return EvidenceRef(
        evidence_id=ref.source_id,
        source_type=EvidenceSourceType.RULE_CATALOG,
        title=ref.notes or ref.source_id,
        locator=ref.locator,
        confidence=max(0.0, min(1.0, confidence)),
        hash=ref.content_hash,
    )


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
    # Memoizes per-rule SHA-256 versions (keyed by the unique rule id). Rules are
    # immutable for the engine's lifetime, so the digest only needs computing
    # once; recomputing the full `model_dump` hash on every `evaluate()` was pure
    # waste. `init=False`/`compare=False` keep equality and construction by
    # `ruleset` alone; mutating the dict is allowed even on a frozen dataclass.
    _version_cache: dict[str, str] = field(default_factory=dict, init=False, compare=False, repr=False)

    @classmethod
    def from_rules_directory(cls, directory: str = RULES_DIR) -> RulesEngine:
        return cls(ruleset=load_rules_from_directory(directory))

    def evaluate(self, case_state: CaseState, recent_facts: list[Fact]) -> RulesEngineResult:
        fiscal_year = self._fiscal_year_from_case(case_state)
        applicable_rules = self._select_applicable_rules(fiscal_year)
        fact_map = self._build_fact_map(case_state, recent_facts)

        traces: list[RuleTrace] = []
        obligations: list[ObligationCandidate] = []
        # rule_id -> obligation_id, so conflict resolution can mark *only* the
        # traces whose obligation actually collided (see _resolve_conflicts).
        rule_obligation_map: dict[str, str] = {}

        for rule in applicable_rules:
            trace, matched = self._evaluate_rule(rule, fact_map)
            traces.append(trace)
            if matched:
                obligations.append(self._build_candidate_obligation(rule, case_state, trace.missing_facts))
                rule_obligation_map[rule.id] = rule.generated_obligation.obligation_id

        deduped_obligations, updated_traces = self._resolve_conflicts(obligations, traces, rule_obligation_map)

        return RulesEngineResult(
            candidate_obligations=deduped_obligations,
            rule_traces=updated_traces,
            ruleset_version=self._ruleset_version(applicable_rules),
            fiscal_year=fiscal_year,
        )

    def _fiscal_year_from_case(self, case_state: CaseState) -> int:
        year = parse_fiscal_year(case_state.tax_period)
        return year if year is not None else datetime.now(UTC).year

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
            try:
                return float(actual_value) >= float(expected)
            except (TypeError, ValueError):
                return False
        if op is ConditionOperator.LTE:
            try:
                return float(actual_value) <= float(expected)
            except (TypeError, ValueError):
                return False
        return False

    def _build_candidate_obligation(
        self, rule: DecisionRule, case_state: CaseState, missing_facts: list[str]
    ) -> ObligationCandidate:
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
            # Carry the rule's normative backing onto the obligation so the
            # audit trail, the explainer's "Fuentes" section and the planner
            # checklist can cite the BOE/AEAT source that justifies the
            # recommendation. Previously hardcoded to ``[]``, which left every
            # engine-generated obligation untraceable.
            evidence_refs=[_source_ref_to_evidence(ref, rule.base_confidence) for ref in rule.source_refs],
            created_at=now,
            updated_at=now,
        )

    def _resolve_conflicts(
        self,
        obligations: list[ObligationCandidate],
        traces: list[RuleTrace],
        rule_obligation_map: dict[str, str],
    ) -> tuple[list[ObligationCandidate], list[RuleTrace]]:
        # Conflict strategy: for same obligation_id keep highest confidence.
        by_id: dict[str, ObligationCandidate] = {}
        counts: dict[str, int] = {}
        for obligation in obligations:
            counts[obligation.obligation_id] = counts.get(obligation.obligation_id, 0) + 1
            existing = by_id.get(obligation.obligation_id)
            if existing is None or obligation.confidence > existing.confidence:
                by_id[obligation.obligation_id] = obligation

        # Only the obligation_ids produced by more than one rule were actually
        # in conflict; mark just those traces so the audit trail stays precise
        # instead of flagging every matched rule.
        conflicted_ids = {obligation_id for obligation_id, count in counts.items() if count > 1}
        if conflicted_ids:
            for trace in traces:
                if trace.matched and rule_obligation_map.get(trace.rule_id) in conflicted_ids:
                    trace.conflict_resolved = True
                    trace.conflict_strategy = "highest_confidence_per_obligation_id"

        return list(by_id.values()), traces

    def _rule_version(self, rule: DecisionRule) -> str:
        cached = self._version_cache.get(rule.id)
        if cached is not None:
            return cached
        payload = rule.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        version = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self._version_cache[rule.id] = version
        return version

    def _ruleset_version(self, rules: list[DecisionRule]) -> str:
        if not rules:
            return "empty"
        versions = sorted(self._rule_version(rule) for rule in rules)
        return hashlib.sha256("|".join(versions).encode("utf-8")).hexdigest()


@lru_cache(maxsize=8)
def _engine_for_directory(rules_directory: str) -> RulesEngine:
    """Build (and reuse) the engine for a rules directory.

    Loading a directory globs, reads and Pydantic-validates every rule file.
    That is static for the process lifetime, so — mirroring the cached case
    store and QA chain — the first turn pays the cost and the rest reuse the
    warm engine instead of re-parsing the rules on every request.
    """
    return RulesEngine.from_rules_directory(rules_directory)


def clear_rules_cache() -> None:
    """Drop the cached engine(s); call after editing rule files in-process."""
    _engine_for_directory.cache_clear()


def evaluate_rules(
    case_state: CaseState, recent_facts: list[Fact], rules_directory: str = RULES_DIR
) -> RulesEngineResult:
    engine = _engine_for_directory(rules_directory)
    return engine.evaluate(case_state=case_state, recent_facts=recent_facts)
