"""Normative drift detection for the decision ruleset.

Each `DecisionRule` declares the normative sources backing it via
`source_refs`. The drift detector resolves every source locator against a
content store (typically the latest crawler snapshot), hashes the bytes,
and compares against the hash recorded the last time the rule was
reviewed. Any mismatch is surfaced so a human can review the rule against
the new normative text before it expires de facto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.decision.rules import DecisionRule, RuleSet, RuleSourceRef


class DriftStatus(str, Enum):
    OK = "ok"
    CHANGED = "changed"
    MISSING = "missing"
    UNVERIFIED = "unverified"


class DriftSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_UNVERIFIED_HASH = "0" * 64


class DriftFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    source_id: str
    locator: str
    status: DriftStatus
    severity: DriftSeverity
    expected_hash: str
    actual_hash: str | None
    last_reviewed_at: date
    detail: str


class DriftSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_sources: int
    ok: int
    changed: int
    missing: int
    unverified: int
    rules_with_drift: list[str] = Field(default_factory=list)
    rules_without_sources: list[str] = Field(default_factory=list)


class DriftReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    ruleset_size: int
    findings: list[DriftFinding]
    summary: DriftSummary

    def has_blocking_drift(self) -> bool:
        return any(
            finding.severity is DriftSeverity.CRITICAL or finding.status is DriftStatus.CHANGED
            for finding in self.findings
        )


class ContentResolver(Protocol):
    """Resolve a normative locator to the raw bytes of the source content."""

    def resolve(self, locator: str) -> bytes | None: ...


@dataclass(frozen=True)
class FilesystemResolver:
    """Resolve locators of the form ``scheme://relative/path`` against a root.

    The scheme is preserved as the first path segment so multiple crawlers
    (boe, aeat, teac, …) can coexist under the same root. Locators without
    a scheme are interpreted as paths relative to ``root``.
    """

    root: Path

    def resolve(self, locator: str) -> bytes | None:
        path = self._locator_to_path(locator)
        if path is None or not path.exists() or not path.is_file():
            return None
        return path.read_bytes()

    def _locator_to_path(self, locator: str) -> Path | None:
        if not locator:
            return None
        if "://" in locator:
            scheme, _, rest = locator.partition("://")
            scheme = scheme.strip().lower()
            rest = rest.strip().lstrip("/")
            if not scheme or not rest:
                return None
            return self.root / scheme / rest
        return self.root / locator.lstrip("/")


def compute_content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class DriftDetector:
    resolver: ContentResolver

    def detect(self, ruleset: RuleSet) -> DriftReport:
        findings: list[DriftFinding] = []
        rules_with_drift: set[str] = set()
        rules_without_sources: list[str] = []

        for rule in ruleset.rules:
            if not rule.source_refs:
                rules_without_sources.append(rule.id)
                continue
            for ref in rule.source_refs:
                finding = self._evaluate_source(rule, ref)
                findings.append(finding)
                if finding.status is not DriftStatus.OK:
                    rules_with_drift.add(rule.id)

        summary = DriftSummary(
            total_sources=len(findings),
            ok=sum(1 for f in findings if f.status is DriftStatus.OK),
            changed=sum(1 for f in findings if f.status is DriftStatus.CHANGED),
            missing=sum(1 for f in findings if f.status is DriftStatus.MISSING),
            unverified=sum(1 for f in findings if f.status is DriftStatus.UNVERIFIED),
            rules_with_drift=sorted(rules_with_drift),
            rules_without_sources=rules_without_sources,
        )
        return DriftReport(
            generated_at=datetime.now(UTC),
            ruleset_size=len(ruleset.rules),
            findings=findings,
            summary=summary,
        )

    def _evaluate_source(self, rule: DecisionRule, ref: RuleSourceRef) -> DriftFinding:
        content = self.resolver.resolve(ref.locator)
        if content is None:
            return DriftFinding(
                rule_id=rule.id,
                source_id=ref.source_id,
                locator=ref.locator,
                status=DriftStatus.MISSING,
                severity=DriftSeverity.WARNING,
                expected_hash=ref.content_hash,
                actual_hash=None,
                last_reviewed_at=ref.last_reviewed_at,
                detail="Locator could not be resolved against the content store",
            )

        actual_hash = compute_content_hash(content)
        if ref.content_hash == _UNVERIFIED_HASH:
            return DriftFinding(
                rule_id=rule.id,
                source_id=ref.source_id,
                locator=ref.locator,
                status=DriftStatus.UNVERIFIED,
                severity=DriftSeverity.WARNING,
                expected_hash=ref.content_hash,
                actual_hash=actual_hash,
                last_reviewed_at=ref.last_reviewed_at,
                detail="Source has never been linked: record the current hash after manual review",
            )
        if actual_hash != ref.content_hash:
            return DriftFinding(
                rule_id=rule.id,
                source_id=ref.source_id,
                locator=ref.locator,
                status=DriftStatus.CHANGED,
                severity=DriftSeverity.CRITICAL,
                expected_hash=ref.content_hash,
                actual_hash=actual_hash,
                last_reviewed_at=ref.last_reviewed_at,
                detail="Source content changed since last review; rule must be re-validated",
            )
        return DriftFinding(
            rule_id=rule.id,
            source_id=ref.source_id,
            locator=ref.locator,
            status=DriftStatus.OK,
            severity=DriftSeverity.INFO,
            expected_hash=ref.content_hash,
            actual_hash=actual_hash,
            last_reviewed_at=ref.last_reviewed_at,
            detail="Source content matches the hash recorded at last review",
        )


def write_report(report: DriftReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
