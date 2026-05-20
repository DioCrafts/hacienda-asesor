from pathlib import Path

from hacienda_gpt.cli.eval_interpreter import evaluate


def test_evaluate_returns_required_metrics() -> None:
    dataset = Path("eval_data/intent_fact_extraction.jsonl")
    metrics = evaluate(dataset)

    assert metrics["total_cases"] >= 100
    for key in [
        "intent_accuracy",
        "fact_precision",
        "fact_recall",
        "fact_f1",
        "missing_fact_detection_recall",
    ]:
        assert key in metrics
        assert 0.0 <= metrics[key] <= 1.0
