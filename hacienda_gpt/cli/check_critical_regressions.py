from __future__ import annotations

import json
from pathlib import Path

import click

from hacienda_gpt.cli.eval_pipeline import compute_metrics, load_eval_data


@click.command()
@click.option("--dataset", default="eval_data/critical_cases/tramite_year_critical_cases.jsonl")
@click.option("--min-intent-accuracy", default=0.75, type=float)
@click.option("--min-fact-f1", default=0.55, type=float)
@click.option("--max-unsafe-rate", default=0.05, type=float)
@click.option("--min-turn-efficiency", default=0.4, type=float)
def cli(
    dataset: str,
    min_intent_accuracy: float,
    min_fact_f1: float,
    max_unsafe_rate: float,
    min_turn_efficiency: float,
) -> None:
    rows = load_eval_data(Path(dataset))
    metrics = compute_metrics(rows)

    failures: list[str] = []
    if metrics["intent_accuracy"] < min_intent_accuracy:
        failures.append(f"intent_accuracy {metrics['intent_accuracy']} < {min_intent_accuracy}")
    if metrics["fact_extraction_f1"] < min_fact_f1:
        failures.append(f"fact_extraction_f1 {metrics['fact_extraction_f1']} < {min_fact_f1}")
    if metrics["unsafe_recommendation_rate"] > max_unsafe_rate:
        failures.append(f"unsafe_recommendation_rate {metrics['unsafe_recommendation_rate']} > {max_unsafe_rate}")
    if metrics["turn_efficiency"] < min_turn_efficiency:
        failures.append(f"turn_efficiency {metrics['turn_efficiency']} < {min_turn_efficiency}")

    click.echo(json.dumps(metrics, ensure_ascii=False, indent=2))

    if failures:
        raise SystemExit("Critical regression checks failed:\n- " + "\n- ".join(failures))


if __name__ == "__main__":
    cli()
