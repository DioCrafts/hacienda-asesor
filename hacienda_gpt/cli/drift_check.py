"""CLI entrypoint for the normative drift detector.

Usage:
    uv run python -m hacienda_gpt.cli.drift_check \\
        --rules-dir rules \\
        --snapshot-root ./data \\
        --output reports/drift.json \\
        [--fail-on-changed]

Exits with status code 2 when ``--fail-on-changed`` is set and the report
contains changed or critical findings. Always exits 0 on success.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import click

from hacienda_gpt.decision.rules import load_rules_from_directory
from hacienda_gpt.governance.drift import DriftDetector, DriftReport, DriftStatus, FilesystemResolver, write_report
from hacienda_gpt.settings import RULES_DIR


def record_unverified_hashes(rules_dir: Path, report: DriftReport) -> int:
    """Link never-verified sources to the snapshot hash the detector just read.

    Rewrites the rule JSON files in place. Only ``UNVERIFIED`` findings are
    applied: accepting a ``CHANGED`` hash would silently bless a law change the
    rule has not been re-validated against.
    """
    pending = {
        (finding.rule_id, finding.source_id): finding.actual_hash
        for finding in report.findings
        if finding.status is DriftStatus.UNVERIFIED and finding.actual_hash
    }
    if not pending:
        return 0
    today = datetime.now(UTC).date().isoformat()
    recorded = 0
    for path in sorted(rules_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data if isinstance(data, list) else data.get("rules", [])
        changed = False
        for rule in rules:
            for ref in rule.get("source_refs", []):
                actual = pending.get((rule.get("id"), ref.get("source_id")))
                if actual:
                    ref["content_hash"] = actual
                    ref["last_reviewed_at"] = today
                    changed = True
                    recorded += 1
        if changed:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return recorded


def _default_report_path() -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return Path("reports") / f"drift_{today}.json"


@click.command()
@click.option("--rules-dir", default=RULES_DIR, show_default=True, help="Directory containing rule JSON files")
@click.option(
    "--snapshot-root",
    default="./data",
    show_default=True,
    help="Root directory where crawler snapshots live, keyed by scheme (boe/, aeat/, …)",
)
@click.option(
    "--output",
    "output",
    default=None,
    help="Output path for the drift report JSON (default: reports/drift_<date>.json)",
)
@click.option(
    "--fail-on-changed",
    is_flag=True,
    default=False,
    help="Exit with non-zero code when changed/critical drift is detected (useful in CI)",
)
@click.option(
    "--record-unverified",
    is_flag=True,
    default=False,
    help=(
        "Write the current snapshot hash into every source_ref still carrying the all-zero placeholder, "
        "stamping last_reviewed_at with today. Use after reviewing that the snapshot is the text the rule was "
        "written against; sources already linked (ok/changed) are never touched."
    ),
)
def cli(rules_dir: str, snapshot_root: str, output: str | None, fail_on_changed: bool, record_unverified: bool) -> None:
    ruleset = load_rules_from_directory(rules_dir)
    detector = DriftDetector(resolver=FilesystemResolver(root=Path(snapshot_root)))
    report = detector.detect(ruleset)

    if record_unverified:
        recorded = record_unverified_hashes(Path(rules_dir), report)
        click.echo(f"Recorded {recorded} source hash(es) into {rules_dir}")
        report = detector.detect(load_rules_from_directory(rules_dir))

    output_path = Path(output) if output else _default_report_path()
    write_report(report, output_path)

    click.echo(json.dumps(report.summary.model_dump(mode="json"), indent=2, ensure_ascii=False))
    click.echo(f"Drift report written to {output_path}")

    if fail_on_changed and report.has_blocking_drift():
        raise SystemExit(2)


if __name__ == "__main__":
    cli()
