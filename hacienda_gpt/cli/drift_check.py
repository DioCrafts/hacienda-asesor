"""CLI entrypoint for the normative drift detector.

Usage:
    poetry run python -m hacienda_gpt.cli.drift_check \\
        --rules-dir rules \\
        --snapshot-root ./data \\
        --output reports/drift.json \\
        [--fail-on-changed]

Exits with status code 2 when ``--fail-on-changed`` is set and the report
contains changed or critical findings. Always exits 0 on success.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click

from hacienda_gpt.decision.rules import load_rules_from_directory
from hacienda_gpt.governance.drift import DriftDetector, FilesystemResolver, write_report


def _default_report_path() -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return Path("reports") / f"drift_{today}.json"


@click.command()
@click.option("--rules-dir", default="rules", show_default=True, help="Directory containing rule JSON files")
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
def cli(rules_dir: str, snapshot_root: str, output: str | None, fail_on_changed: bool) -> None:
    ruleset = load_rules_from_directory(rules_dir)
    detector = DriftDetector(resolver=FilesystemResolver(root=Path(snapshot_root)))
    report = detector.detect(ruleset)

    output_path = Path(output) if output else _default_report_path()
    write_report(report, output_path)

    click.echo(json.dumps(report.summary.model_dump(mode="json"), indent=2, ensure_ascii=False))
    click.echo(f"Drift report written to {output_path}")

    if fail_on_changed and report.has_blocking_drift():
        raise SystemExit(2)


if __name__ == "__main__":
    cli()
