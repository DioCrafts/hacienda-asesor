from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from hacienda_gpt.decision.rules import DecisionRule, RuleSet, RuleSourceRef
from hacienda_gpt.governance.drift import (
    DriftDetector,
    DriftSeverity,
    DriftStatus,
    FilesystemResolver,
    compute_content_hash,
    write_report,
)


def _rule(rule_id: str, source_refs: list[RuleSourceRef] | None = None) -> DecisionRule:
    return DecisionRule.model_validate(
        {
            "id": rule_id,
            "jurisdiction": "ES",
            "valid_from": "2024-01-01",
            "valid_to": "2026-12-31",
            "conditions": [{"fact": "x", "operator": "exists"}],
            "required_facts": ["x"],
            "generated_obligation": {
                "obligation_id": f"obl_{rule_id}",
                "title": "t",
                "description": "d",
                "status": "candidate",
            },
            "base_confidence": 0.5,
            "risk_level": "medium",
            "source_refs": [ref.model_dump(mode="json") for ref in (source_refs or [])],
            "last_reviewed_at": "2024-12-01",
        }
    )


def _write_source(root: Path, scheme: str, rel_path: str, content: bytes) -> None:
    target = root / scheme / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


class _DictResolver:
    def __init__(self, mapping: dict[str, bytes | None]) -> None:
        self._mapping = mapping

    def resolve(self, locator: str) -> bytes | None:
        return self._mapping.get(locator)


def test_detector_reports_ok_when_hash_matches() -> None:
    content = b"Articulo 1: contenido normativo"
    ref = RuleSourceRef(
        source_id="BOE-X",
        locator="boe://BOE-X",
        content_hash=compute_content_hash(content),
        last_reviewed_at=date(2024, 12, 1),
    )
    ruleset = RuleSet(rules=[_rule("r_ok", [ref])])
    detector = DriftDetector(resolver=_DictResolver({"boe://BOE-X": content}))

    report = detector.detect(ruleset)

    assert len(report.findings) == 1
    assert report.findings[0].status is DriftStatus.OK
    assert report.findings[0].severity is DriftSeverity.INFO
    assert report.summary.ok == 1
    assert not report.has_blocking_drift()


def test_detector_reports_changed_when_hash_differs() -> None:
    ref = RuleSourceRef(
        source_id="BOE-X",
        locator="boe://BOE-X",
        content_hash=compute_content_hash(b"viejo"),
        last_reviewed_at=date(2024, 12, 1),
    )
    ruleset = RuleSet(rules=[_rule("r_drift", [ref])])
    detector = DriftDetector(resolver=_DictResolver({"boe://BOE-X": b"nuevo"}))

    report = detector.detect(ruleset)

    assert report.findings[0].status is DriftStatus.CHANGED
    assert report.findings[0].severity is DriftSeverity.CRITICAL
    assert report.findings[0].actual_hash == compute_content_hash(b"nuevo")
    assert report.summary.changed == 1
    assert "r_drift" in report.summary.rules_with_drift
    assert report.has_blocking_drift()


def test_detector_reports_missing_when_locator_unresolved() -> None:
    ref = RuleSourceRef(
        source_id="BOE-X",
        locator="boe://BOE-X",
        content_hash=compute_content_hash(b"x"),
        last_reviewed_at=date(2024, 12, 1),
    )
    ruleset = RuleSet(rules=[_rule("r_missing", [ref])])
    detector = DriftDetector(resolver=_DictResolver({}))

    report = detector.detect(ruleset)

    assert report.findings[0].status is DriftStatus.MISSING
    assert report.findings[0].severity is DriftSeverity.WARNING
    assert report.summary.missing == 1


def test_detector_reports_unverified_for_placeholder_hash() -> None:
    ref = RuleSourceRef(
        source_id="BOE-X",
        locator="boe://BOE-X",
        content_hash="0" * 64,
        last_reviewed_at=date(2024, 12, 1),
    )
    ruleset = RuleSet(rules=[_rule("r_unverified", [ref])])
    detector = DriftDetector(resolver=_DictResolver({"boe://BOE-X": b"contenido"}))

    report = detector.detect(ruleset)

    assert report.findings[0].status is DriftStatus.UNVERIFIED
    assert report.findings[0].severity is DriftSeverity.WARNING
    assert report.findings[0].actual_hash == compute_content_hash(b"contenido")


def test_detector_flags_rules_without_source_refs() -> None:
    ruleset = RuleSet(rules=[_rule("r_orphan", [])])
    detector = DriftDetector(resolver=_DictResolver({}))

    report = detector.detect(ruleset)

    assert report.findings == []
    assert report.summary.rules_without_sources == ["r_orphan"]


def test_filesystem_resolver_reads_scheme_prefixed_locator(tmp_path: Path) -> None:
    _write_source(tmp_path, "boe", "BOE-X.html", b"contenido")
    resolver = FilesystemResolver(root=tmp_path)

    assert resolver.resolve("boe://BOE-X.html") == b"contenido"
    assert resolver.resolve("boe://missing.html") is None
    assert resolver.resolve("") is None


def test_filesystem_resolver_supports_relative_locator(tmp_path: Path) -> None:
    target = tmp_path / "shared.json"
    target.write_bytes(b"hello")
    resolver = FilesystemResolver(root=tmp_path)

    assert resolver.resolve("shared.json") == b"hello"


def test_write_report_serializes_to_disk(tmp_path: Path) -> None:
    content = b"contenido"
    ref = RuleSourceRef(
        source_id="BOE-X",
        locator="boe://BOE-X",
        content_hash=compute_content_hash(content),
        last_reviewed_at=date(2024, 12, 1),
    )
    ruleset = RuleSet(rules=[_rule("r_ok", [ref])])
    detector = DriftDetector(resolver=_DictResolver({"boe://BOE-X": content}))
    report = detector.detect(ruleset)

    output = tmp_path / "out" / "drift.json"
    write_report(report, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"]["ok"] == 1
    assert payload["findings"][0]["status"] == "ok"
