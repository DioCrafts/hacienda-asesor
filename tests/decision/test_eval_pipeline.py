from pathlib import Path

from hacienda_gpt.cli.eval_pipeline import build_html_report, build_markdown_report, compute_metrics, load_eval_data


def test_eval_pipeline_metrics_and_reports() -> None:
    rows = load_eval_data(Path("eval_data/intent_fact_extraction.jsonl"))
    metrics = compute_metrics(rows)

    required = {
        "intent_accuracy",
        "fact_extraction_f1",
        "obligation_precision",
        "obligation_recall",
        "grounded_citation_rate",
        "unsafe_recommendation_rate",
        "turn_efficiency",
    }
    assert required.issubset(metrics.keys())

    md = build_markdown_report(metrics)
    html = build_html_report(metrics)

    assert "Evaluation Pipeline Report" in md
    assert "<html" in html.lower()
    assert "tendencias" in md.lower()
