from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

import click

from hacienda_gpt.decision.fact_extractor import default_fact_extractor
from hacienda_gpt.decision.interpreter import interpret_turn
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState

# Why this CLI runs the model instead of reading predictions from the dataset:
# the critical-cases JSONL ships GOLD labels only. The previous implementation
# loaded it through ``eval_pipeline.load_eval_data``, whose ``pred_*`` fields
# default to the gold values when absent — so intent_accuracy and fact_f1 were
# 1.0 *by construction* and the CI gate passed no matter how badly the real
# extractor / interpreter / rules engine regressed. Here we EXECUTE the actual
# deterministic pipeline (``interpret_turn`` → ``evaluate_rules``) on each
# case's input and score those real predictions.
#
# In CI (no OPENAI_API_KEY) ``default_fact_extractor`` returns the deterministic
# ``RegexFactExtractor``. That path reliably classifies intent and extracts the
# coarse facts, so intent_accuracy and fact_f1 are meaningful gates that catch
# regressions in the interpreter, the intent taxonomy or the rule loader.
# Obligation RECALL needs the richer LLM extractor (the regex one never emits
# ``tipo_renta`` / ``alta_actividad_economica``, so rules don't fire) — it is
# reported but NOT gated here. What IS gated as a safety floor is the rate of
# FABRICATED obligations (predicted but not in gold): an over-broad
# recommendation is the dangerous failure mode for a fiscal advisor, and it
# must stay at zero on both the regex and the LLM path.


@dataclass
class CaseScore:
    case_id: str
    pred_intent: str
    gold_intent: str
    pred_facts: set[str]
    gold_facts: set[str]
    pred_obligations: set[str]
    gold_obligations: set[str]


def _predict(row: dict, extractor) -> CaseScore:
    now = datetime.now(UTC)
    case = CaseState(
        case_id=str(row.get("case_id", "eval")),
        user_id="eval",
        jurisdiction="ES",
        tax_period=str(row.get("fiscal_year") or "2024"),
        created_at=now,
        updated_at=now,
    )
    interpretation = interpret_turn(row["input"], [], case, extractor=extractor)
    case_for_rules = case.model_copy(update={"facts": interpretation.all_facts})
    rules_result = evaluate_rules(case_state=case_for_rules, recent_facts=interpretation.extracted_facts)
    return CaseScore(
        case_id=case.case_id,
        pred_intent=interpretation.intent.value,
        gold_intent=row.get("gold_intent", "unknown"),
        pred_facts={f.name for f in interpretation.all_facts},
        gold_facts=set(row.get("gold_facts", [])),
        pred_obligations={o.obligation_id for o in rules_result.candidate_obligations},
        gold_obligations=set(row.get("gold_obligations", [])),
    )


def _f1(tp: int, fp: int, fn: int) -> float:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def evaluate_dataset(path: Path, extractor) -> dict[str, float]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise click.ClickException(f"No critical cases found in {path}")

    scores = [_predict(row, extractor) for row in rows]
    total = len(scores)

    intent_ok = sum(1 for s in scores if s.pred_intent == s.gold_intent)
    f_tp = sum(len(s.gold_facts & s.pred_facts) for s in scores)
    f_fp = sum(len(s.pred_facts - s.gold_facts) for s in scores)
    f_fn = sum(len(s.gold_facts - s.pred_facts) for s in scores)
    o_tp = sum(len(s.gold_obligations & s.pred_obligations) for s in scores)
    o_fp = sum(len(s.pred_obligations - s.gold_obligations) for s in scores)
    o_fn = sum(len(s.gold_obligations - s.pred_obligations) for s in scores)
    # A case where the engine recommended an obligation that is not in gold.
    fabricated = sum(1 for s in scores if s.pred_obligations - s.gold_obligations)

    return {
        "total_cases": float(total),
        "intent_accuracy": round(intent_ok / total, 4),
        "fact_extraction_f1": round(_f1(f_tp, f_fp, f_fn), 4),
        "obligation_recall": round(o_tp / (o_tp + o_fn), 4) if (o_tp + o_fn) else 0.0,
        "obligation_precision": round(o_tp / (o_tp + o_fp), 4) if (o_tp + o_fp) else 0.0,
        "unsafe_recommendation_rate": round(fabricated / total, 4),
    }


@click.command()
@click.option("--dataset", default="eval_data/critical_cases/tramite_year_critical_cases.jsonl")
@click.option("--min-intent-accuracy", default=0.80, type=float)
@click.option("--min-fact-f1", default=0.75, type=float)
@click.option("--max-unsafe-rate", default=0.05, type=float)
def cli(
    dataset: str,
    min_intent_accuracy: float,
    min_fact_f1: float,
    max_unsafe_rate: float,
) -> None:
    extractor = default_fact_extractor()
    metrics = evaluate_dataset(Path(dataset), extractor)

    failures: list[str] = []
    if metrics["intent_accuracy"] < min_intent_accuracy:
        failures.append(f"intent_accuracy {metrics['intent_accuracy']} < {min_intent_accuracy}")
    if metrics["fact_extraction_f1"] < min_fact_f1:
        failures.append(f"fact_extraction_f1 {metrics['fact_extraction_f1']} < {min_fact_f1}")
    if metrics["unsafe_recommendation_rate"] > max_unsafe_rate:
        failures.append(f"unsafe_recommendation_rate {metrics['unsafe_recommendation_rate']} > {max_unsafe_rate}")

    click.echo(json.dumps({"extractor": type(extractor).__name__, "metrics": metrics}, ensure_ascii=False, indent=2))

    if failures:
        raise SystemExit("Critical regression checks failed:\n- " + "\n- ".join(failures))


if __name__ == "__main__":
    cli()
