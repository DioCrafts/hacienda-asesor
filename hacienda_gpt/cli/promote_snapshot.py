from __future__ import annotations

import json
import shutil
from pathlib import Path

import click


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_eval_metrics(snapshot_dir: Path) -> dict:
    metrics_path = snapshot_dir / "eval_pipeline_results.json"
    if not metrics_path.exists():
        raise FileNotFoundError(f"Missing metrics file: {metrics_path}")
    return json.loads(metrics_path.read_text(encoding="utf-8"))


def _assert_quality(metrics: dict, gates: dict) -> None:
    failures = []
    if metrics.get("intent_accuracy", 0.0) < gates.get("min_intent_accuracy", 0.0):
        failures.append("intent_accuracy below threshold")
    if metrics.get("unsafe_recommendation_rate", 1.0) > gates.get("max_unsafe_recommendation_rate", 1.0):
        failures.append("unsafe_recommendation_rate above threshold")
    if failures:
        raise SystemExit("Quality gates failed: " + "; ".join(failures))


@click.command()
@click.option("--env", "environment", required=True, type=click.Choice(["dev", "staging", "prod"]))
@click.option("--snapshot", required=True, help="Snapshot date (YYYY-MM-DD)")
@click.option("--config", default="ops/promotion_config.json")
def cli(environment: str, snapshot: str, config: str) -> None:
    cfg = _load_config(Path(config))
    env_cfg = cfg["environments"][environment]

    data_root = Path(env_cfg["data_root"])
    snapshot_dir = data_root / "snapshots" / snapshot
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_dir}")

    metrics = _load_eval_metrics(snapshot_dir)
    _assert_quality(metrics, env_cfg["quality_gates"])

    active_link = data_root / "current"
    previous_link = data_root / "previous"

    if active_link.exists() or active_link.is_symlink():
        if previous_link.exists() or previous_link.is_symlink():
            previous_link.unlink()
        previous_link.symlink_to(active_link.resolve())
        active_link.unlink()

    active_link.symlink_to(snapshot_dir.resolve())

    click.echo(f"Promoted snapshot {snapshot} to {environment}")
    click.echo(f"Active -> {active_link.resolve()}")


if __name__ == "__main__":
    cli()
