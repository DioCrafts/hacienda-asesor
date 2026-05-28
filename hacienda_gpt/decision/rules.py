from __future__ import annotations

import json
import re
from datetime import date
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hacienda_gpt.decision.schemas import ObligationStatus, RiskLevel

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ConditionOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    GTE = "gte"
    LTE = "lte"
    EXISTS = "exists"


class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(min_length=1)
    operator: ConditionOperator
    value: str | float | bool | list[str] | list[float] | list[bool] | None = None

    @model_validator(mode="after")
    def validate_value_for_operator(self) -> RuleCondition:
        if self.operator is ConditionOperator.EXISTS:
            return self
        if self.value is None:
            raise ValueError("value is required for non-exists operators")
        if self.operator is ConditionOperator.IN and not isinstance(self.value, list):
            raise ValueError("value must be a list for 'in' operator")
        return self


class GeneratedObligationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obligation_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: ObligationStatus = ObligationStatus.CANDIDATE


class RuleSourceRef(BaseModel):
    """Normative source backing a rule, used by the drift detector.

    `content_hash` is the SHA-256 of the source bytes at `last_reviewed_at`.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    last_reviewed_at: date
    notes: str | None = None

    @model_validator(mode="after")
    def validate_hash_format(self) -> RuleSourceRef:
        if not _SHA256_HEX_RE.match(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        return self


class DecisionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=2)
    valid_from: date
    valid_to: date
    conditions: list[RuleCondition] = Field(min_length=1)
    required_facts: list[str] = Field(min_length=1)
    generated_obligation: GeneratedObligationSpec
    base_confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    source_refs: list[RuleSourceRef] = Field(default_factory=list)
    last_reviewed_at: date | None = None

    @model_validator(mode="after")
    def validate_rule_integrity(self) -> DecisionRule:
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")
        if len(self.required_facts) != len(set(self.required_facts)):
            raise ValueError("required_facts contains duplicates")
        if any(not fact for fact in self.required_facts):
            raise ValueError("required_facts cannot contain empty values")
        source_ids = [ref.source_id for ref in self.source_refs]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_refs must have unique source_id")
        return self


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[DecisionRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> RuleSet:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self


def load_rules_from_json(path: str | Path) -> RuleSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return RuleSet(rules=[DecisionRule.model_validate(item) for item in data])
    if isinstance(data, dict) and "rules" in data:
        return RuleSet.model_validate(data)
    raise ValueError("Invalid rules JSON format: expected list or object with 'rules'")


def load_rules_from_directory(directory: str | Path) -> RuleSet:
    paths = sorted(Path(directory).glob("*.json"))
    all_rules: list[DecisionRule] = []
    for path in paths:
        ruleset = load_rules_from_json(path)
        all_rules.extend(ruleset.rules)
    return RuleSet(rules=all_rules)
