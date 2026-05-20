from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import click


@dataclass
class EvalExample:
    intent_gold: str
    intent_pred: str
    facts_gold: set[str]
    facts_pred: set[str]
    obligations_gold: set[str]
    obligations_pred: set[str]
    has_grounded_citation: bool
    unsafe_recommendation: bool
    turns: int


def _safe_div(n: int | float, d: int | float) -> float:
    return 0.0 if d == 0 else float(n) / float(d)


def _f1(tp: int, fp: int, fn: int) -> float:
    p = _safe_div(tp, tp + fp)
    r = _safe_div(tp, tp + fn)
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def load_eval_data(path: Path) -> list[EvalExample]:
    rows: list[EvalExample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        rows.append(
            EvalExample(
                intent_gold=obj.get("gold_intent", "unknown"),
                intent_pred=obj.get("pred_intent", obj.get("gold_intent", "unknown")),
                facts_gold=set(obj.get("gold_facts", [])),
                facts_pred=set(obj.get("pred_facts", obj.get("gold_facts", []))),
                obligations_gold=set(obj.get("gold_obligations", [])),
                obligations_pred=set(obj.get("pred_obligations", [])),
                has_grounded_citation=bool(obj.get("has_grounded_citation", False)),
                unsafe_recommendation=bool(obj.get("unsafe_recommendation", False)),
                turns=int(obj.get("turns", 1)),
            )
        )
    return rows


def compute_metrics(rows: list[EvalExample]) -> dict[str, float]:
    total = len(rows)
    intent_ok = sum(1 for r in rows if r.intent_gold == r.intent_pred)

    fact_tp = sum(len(r.facts_gold & r.facts_pred) for r in rows)
    fact_fp = sum(len(r.facts_pred - r.facts_gold) for r in rows)
    fact_fn = sum(len(r.facts_gold - r.facts_pred) for r in rows)

    obl_tp = sum(len(r.obligations_gold & r.obligations_pred) for r in rows)
    obl_fp = sum(len(r.obligations_pred - r.obligations_gold) for r in rows)
    obl_fn = sum(len(r.obligations_gold - r.obligations_pred) for r in rows)

    grounded_rate = _safe_div(sum(1 for r in rows if r.has_grounded_citation), total)
    unsafe_rate = _safe_div(sum(1 for r in rows if r.unsafe_recommendation), total)
    avg_turns = _safe_div(sum(r.turns for r in rows), total)

    return {
        "total_cases": total,
        "intent_accuracy": round(_safe_div(intent_ok, total), 4),
        "fact_extraction_f1": round(_f1(fact_tp, fact_fp, fact_fn), 4),
        "obligation_precision": round(_safe_div(obl_tp, obl_tp + obl_fp), 4),
        "obligation_recall": round(_safe_div(obl_tp, obl_tp + obl_fn), 4),
        "grounded_citation_rate": round(grounded_rate, 4),
        "unsafe_recommendation_rate": round(unsafe_rate, 4),
        "turn_efficiency": round(_safe_div(1.0, avg_turns) if avg_turns else 0.0, 4),
    }


def build_markdown_report(metrics: dict[str, float]) -> str:
    return "\n".join(
        [
            "# Evaluation Pipeline Report",
            "",
            "## Metrics",
            "| Metric | Value |",
            "|---|---:|",
            f"| total_cases | {metrics['total_cases']} |",
            f"| intent_accuracy | {metrics['intent_accuracy']} |",
            f"| fact_extraction_f1 | {metrics['fact_extraction_f1']} |",
            f"| obligation_precision | {metrics['obligation_precision']} |",
            f"| obligation_recall | {metrics['obligation_recall']} |",
            f"| grounded_citation_rate | {metrics['grounded_citation_rate']} |",
            f"| unsafe_recommendation_rate | {metrics['unsafe_recommendation_rate']} |",
            f"| turn_efficiency | {metrics['turn_efficiency']} |",
            "",
            "## Tendencias",
            "- Tendencia de calidad: comparar este reporte con ejecuciones previas por timestamp/branch.",
            "- Si `unsafe_recommendation_rate` sube, bloquear release y revisar reglas/prompt/security.",
            "- Si `turn_efficiency` baja, revisar `question_policy` y cobertura de facts críticos.",
        ]
    )


def build_html_report(metrics: dict[str, float]) -> str:
    rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in metrics.items()
    )
    return f"""
<!doctype html>
<html lang='es'>
<head><meta charset='utf-8'><title>Evaluation Pipeline Report</title></head>
<body>
  <h1>Evaluation Pipeline Report</h1>
  <table border='1' cellspacing='0' cellpadding='6'>
    <thead><tr><th>Métrica</th><th>Valor</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Tendencias</h2>
  <ul>
    <li>Comparar series históricas por ejecución para detectar regresiones.</li>
    <li>Controlar unsafe recommendation rate como guardrail de seguridad.</li>
    <li>Mejorar turn efficiency optimizando question policy y completitud de facts.</li>
  </ul>
</body>
</html>
""".strip()


@click.command()
@click.option("--dataset", default="eval_data/intent_fact_extraction.jsonl")
@click.option("--output-json", default="./eval_pipeline_results.json")
@click.option("--output-md", default="./eval_pipeline_report.md")
@click.option("--output-html", default="./eval_pipeline_report.html")
def cli(dataset: str, output_json: str, output_md: str, output_html: str) -> None:
    rows = load_eval_data(Path(dataset))
    metrics = compute_metrics(rows)

    Path(output_json).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(output_md).write_text(build_markdown_report(metrics), encoding="utf-8")
    Path(output_html).write_text(build_html_report(metrics), encoding="utf-8")

    click.echo(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
