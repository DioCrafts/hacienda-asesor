from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"


class CaseStatus(str, Enum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    CLOSED = "closed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ObligationStatus(str, Enum):
    CANDIDATE = "candidate"
    LIKELY = "likely"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class FactValueType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    OBJECT = "object"
    ARRAY = "array"


class EvidenceSourceType(str, Enum):
    RETRIEVED_DOCUMENT = "retrieved_document"
    USER_PROVIDED_DOCUMENT = "user_provided_document"
    USER_STATEMENT = "user_statement"
    RULE_CATALOG = "rule_catalog"


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: str | float | bool | date | dict | list
    value_type: FactValueType
    source: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MissingFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    priority: RiskLevel


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION)
    evidence_id: str = Field(min_length=1)
    source_type: EvidenceSourceType
    title: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    document_type: str | None = None
    section: str | None = None
    snippet: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = Field(ge=0.0, le=1.0)
    hash: str | None = None


class ObligationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION)
    obligation_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=2)
    tax_period: str = Field(min_length=1)
    status: ObligationStatus = ObligationStatus.CANDIDATE
    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    trigger_facts: list[str] = Field(default_factory=list)
    blocking_missing_facts: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_fact_lists(self) -> ObligationCandidate:
        if any(not item for item in self.trigger_facts):
            raise ValueError("trigger_facts cannot contain empty values")
        if any(not item for item in self.blocking_missing_facts):
            raise ValueError("blocking_missing_facts cannot contain empty values")
        return self


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    priority: int = Field(ge=1)
    risk_level: RiskLevel
    due_date: date | None = None
    depends_on: list[str] = Field(default_factory=list)
    expected_outcome: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_depends_on(self) -> ActionItem:
        if self.action_id in self.depends_on:
            raise ValueError("depends_on cannot include itself")
        if any(not dep for dep in self.depends_on):
            raise ValueError("depends_on cannot contain empty values")
        return self


class CaseState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION)
    case_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    status: CaseStatus = CaseStatus.OPEN
    jurisdiction: str = Field(min_length=2)
    tax_period: str = Field(min_length=1)
    facts: list[Fact] = Field(default_factory=list)
    missing_facts: list[MissingFact] = Field(default_factory=list)
    obligation_candidates: list[ObligationCandidate] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("facts")
    @classmethod
    def validate_unique_fact_ids(cls, facts: list[Fact]) -> list[Fact]:
        ids = [fact.fact_id for fact in facts]
        if len(ids) != len(set(ids)):
            raise ValueError("facts must have unique fact_id")
        return facts

    @model_validator(mode="after")
    def validate_temporal_integrity(self) -> CaseState:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        missing_names = {missing.fact_name for missing in self.missing_facts}
        known_facts = {fact.name for fact in self.facts}
        overlap = missing_names.intersection(known_facts)
        if overlap:
            raise ValueError(f"facts and missing_facts overlap: {sorted(overlap)}")
        return self


class DecisionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default=SCHEMA_VERSION)
    case_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    obligations: list[ObligationCandidate] = Field(default_factory=list)
    action_plan: list[ActionItem] = Field(default_factory=list)
    missing_facts: list[MissingFact] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_action_plan_integrity(self) -> DecisionOutput:
        action_ids = [action.action_id for action in self.action_plan]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action_plan must have unique action_id")
        valid_ids = set(action_ids)
        for action in self.action_plan:
            unknown = [dep for dep in action.depends_on if dep not in valid_ids]
            if unknown:
                raise ValueError(f"action {action.action_id} depends on unknown actions: {unknown}")
        return self
