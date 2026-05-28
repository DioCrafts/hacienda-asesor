from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hacienda_gpt.cli.drift_check import cli


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_rule_with_source(rules_dir: Path, *, hash_value: str) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": "r_cli",
            "jurisdiction": "ES",
            "valid_from": "2024-01-01",
            "valid_to": "2026-12-31",
            "conditions": [{"fact": "x", "operator": "exists"}],
            "required_facts": ["x"],
            "generated_obligation": {
                "obligation_id": "obl_cli",
                "title": "t",
                "description": "d",
                "status": "candidate",
            },
            "base_confidence": 0.5,
            "risk_level": "medium",
            "last_reviewed_at": "2024-12-01",
            "source_refs": [
                {
                    "source_id": "BOE-X",
                    "locator": "boe://BOE-X.html",
                    "content_hash": hash_value,
                    "last_reviewed_at": "2024-12-01",
                }
            ],
        }
    ]
    (rules_dir / "cli_rules.json").write_text(json.dumps(payload), encoding="utf-8")


def test_cli_writes_report_and_exits_zero_when_no_drift(tmp_path: Path) -> None:
    content = b"contenido normativo"
    snapshot_root = tmp_path / "snapshots"
    (snapshot_root / "boe").mkdir(parents=True)
    (snapshot_root / "boe" / "BOE-X.html").write_bytes(content)

    rules_dir = tmp_path / "rules"
    _write_rule_with_source(rules_dir, hash_value=_sha(content))

    output = tmp_path / "drift.json"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--rules-dir",
            str(rules_dir),
            "--snapshot-root",
            str(snapshot_root),
            "--output",
            str(output),
            "--fail-on-changed",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["ok"] == 1


def test_cli_exits_two_when_drift_detected_and_flag_enabled(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    (snapshot_root / "boe").mkdir(parents=True)
    (snapshot_root / "boe" / "BOE-X.html").write_bytes(b"nuevo contenido")

    rules_dir = tmp_path / "rules"
    _write_rule_with_source(rules_dir, hash_value=_sha(b"viejo"))

    output = tmp_path / "drift.json"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--rules-dir",
            str(rules_dir),
            "--snapshot-root",
            str(snapshot_root),
            "--output",
            str(output),
            "--fail-on-changed",
        ],
    )

    assert result.exit_code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["changed"] == 1


def test_cli_exits_zero_when_drift_detected_but_flag_disabled(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    (snapshot_root / "boe").mkdir(parents=True)
    (snapshot_root / "boe" / "BOE-X.html").write_bytes(b"nuevo contenido")

    rules_dir = tmp_path / "rules"
    _write_rule_with_source(rules_dir, hash_value=_sha(b"viejo"))

    output = tmp_path / "drift.json"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--rules-dir", str(rules_dir), "--snapshot-root", str(snapshot_root), "--output", str(output)],
    )

    assert result.exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["changed"] == 1
