from __future__ import annotations

import json
from pathlib import Path

import click

from hacienda_gpt.decision.interpreter import interpret_turn


def _safe_div(num: int, den: int) -> float:
    return 0.0 if den == 0 else num / den


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def evaluate(dataset_path: Path) -> dict:
    total = 0
    correct_intent = 0

    tp = 0
    fp = 0
    fn = 0

    missing_tp = 0
    missing_total = 0

    with dataset_path.open("r", encoding="utf-8") as fp_file:
        for line in fp_file:
            if not line.strip():
                continue
            row = json.loads(line)
            total += 1

            result = interpret_turn(
                user_input=row["user_input"],
                chat_history=row.get("chat_history", []),
                current_case_state=None,
            )

            pred_intent = result.intent.value
            if pred_intent == row["gold_intent"]:
                correct_intent += 1

            pred_facts = {fact.name for fact in result.extracted_facts}
            gold_facts = set(row.get("gold_facts", []))
            tp += len(pred_facts & gold_facts)
            fp += len(pred_facts - gold_facts)
            fn += len(gold_facts - pred_facts)

            pred_missing = {m.fact_name for m in result.missing_facts}
            gold_missing = set(row.get("gold_missing_facts", []))
            missing_tp += len(pred_missing & gold_missing)
            missing_total += len(gold_missing)

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)

    metrics = {
        "total_cases": total,
        "intent_accuracy": round(_safe_div(correct_intent, total), 4),
        "fact_precision": round(precision, 4),
        "fact_recall": round(recall, 4),
        "fact_f1": round(_f1(precision, recall), 4),
        "missing_fact_detection_recall": round(_safe_div(missing_tp, missing_total), 4),
    }
    return metrics


def _to_markdown(metrics: dict) -> str:
    return "\n".join(
        [
            "# Eval Interpreter Results",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| total_cases | {metrics['total_cases']} |",
            f"| intent_accuracy | {metrics['intent_accuracy']} |",
            f"| fact_precision | {metrics['fact_precision']} |",
            f"| fact_recall | {metrics['fact_recall']} |",
            f"| fact_f1 | {metrics['fact_f1']} |",
            f"| missing_fact_detection_recall | {metrics['missing_fact_detection_recall']} |",
        ]
    )


@click.command()
@click.option("--dataset", default="eval_data/intent_fact_extraction.jsonl", help="Path to jsonl dataset")
@click.option("--output-json", default="./eval_interpreter_results.json", help="Path output JSON")
@click.option("--output-md", default="./eval_interpreter_results.md", help="Path output markdown")
def cli(dataset: str, output_json: str, output_md: str) -> None:
    dataset_path = Path(dataset)
    metrics = evaluate(dataset_path)

    Path(output_json).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(output_md).write_text(_to_markdown(metrics), encoding="utf-8")

    click.echo(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
