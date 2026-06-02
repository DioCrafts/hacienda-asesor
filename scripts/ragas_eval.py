"""RAGAS evaluation driven by a **local** MLX Qwen3 judge.

Runs the 15-scenario QA battery through the production chain, then asks
RAGAS to score each response on three reference-free metrics:

    faithfulness                — does the answer stay within the retrieved context?
    answer_relevancy            — does the answer address the user's question?
    context_precision (no-ref)  — were the retrieved chunks actually useful?

The judge LLM is the same Qwen3-1.7B (MLX bf16) we use for chunk
contextualisation, wrapped via :class:`MLXChatLLM`. The judge embedding
model is the same Qwen3-Embedding-0.6B (MLX bf16) we use for retrieval.
**No OpenAI calls are made.** Quality of a 1.7 B-param judge is lower
than gpt-4o-mini, but consistent and free — adequate for A/B comparing
two of our own retrieval configurations against each other.

Typical usage::

    OMP_NUM_THREADS=1 FAISS_INDEX_PATH=./data/faiss-test \
        uv run python scripts/ragas_eval.py \
        --out /tmp/hacienda-run/ragas_baseline.json \
        --label baseline-reranker-on

Pair an ``--out`` path with a ``--label`` per configuration. Compare
runs by reading the per-scenario metric tables — RAGAS scores are
noisy at sample sizes this small, but a clear regression on
``faithfulness`` or ``context_precision`` is still a strong signal.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Make both ``hacienda_gpt`` (project root) and ``qa_battery_test``
# (sibling script) importable regardless of how Python was invoked.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # for qa_battery_test
sys.path.insert(0, str(_HERE.parent))  # for hacienda_gpt

# Reuse the canonical scenario list so RAGAS evaluates exactly the same
# user prompts that the human-review QA battery already covers. Keep the
# two scripts pointed at the same source of truth.
from qa_battery_test import QUERIES  # type: ignore  # noqa: E402

logger = logging.getLogger(__name__)


def _build_chain():
    """Construct the production chain. We rely on the standard env vars
    (``FAISS_INDEX_PATH``, ``OPENAI_API_KEY``) — the LLM behind the chain
    is still OpenAI (because the user-facing chain quality matters and
    matches what real users would see); only the **judge** runs locally.
    """
    from hacienda_gpt.llm.chain import create_openai_chain

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required: the chain under test still uses "
            "OpenAI for generation; the local MLX model is only the judge."
        )
    return create_openai_chain(api_key, profile_name="explain")


def _run_scenario(chain, scenario: dict[str, str]) -> dict[str, Any]:
    """Run one query through the chain and capture {question, answer,
    contexts} for RAGAS. We use the raw ``chain.invoke`` (not
    ``answer_with_grounding``) so we can read the un-gated raw answer
    and the full retrieved context list — RAGAS scores the LLM's
    behaviour, not the safety gate's."""
    t0 = time.time()
    result = chain.invoke({"input": scenario["q"], "chat_history": []})
    elapsed = time.time() - t0

    answer = result.get("answer", "") if isinstance(result, dict) else ""
    contexts = []
    if isinstance(result, dict):
        for doc in result.get("context", []) or []:
            content = getattr(doc, "page_content", None)
            if isinstance(content, str) and content.strip():
                contexts.append(content)
    return {
        "id": scenario["id"],
        "user_input": scenario["q"],
        "response": answer or "(empty)",
        "retrieved_contexts": contexts or ["(none)"],  # RAGAS chokes on []
        "elapsed_s": round(elapsed, 2),
    }


def _build_judge():
    """Wire up the local MLX Qwen3 as the RAGAS judge LLM, and our
    existing MLX Qwen3-Embedding-0.6B as the RAGAS judge embedder.
    Both go through LangChain wrappers since RAGAS 0.4.x still accepts
    them via the legacy ``LangchainLLMWrapper`` / ``LangchainEmbeddingsWrapper``.
    The newer ``ragas.metrics.collections`` API requires Instructor-style
    LLMs which would force an OpenAI client; we explicitly want to avoid
    that here."""
    from hacienda_gpt.llm.embeddings import create_embeddings
    from hacienda_gpt.llm.mlx_chat import MLXChatLLM
    from hacienda_gpt import settings as S
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    # The judge model and its token budget are configured separately from
    # the contextualizer (see ``settings.RAGAS_JUDGE_MODEL_PATH``). The
    # default is Qwen3-4B-Instruct-2507 — a stronger checkpoint than the
    # 1.7B contextualizer, justified by the judge's low volume (~15-50
    # scenarios per eval) and high quality bar (structured JSON, yes/no
    # verdicts that drive RAG comparison decisions).
    judge_llm = MLXChatLLM(
        model_path=S.RAGAS_JUDGE_MODEL_PATH,
        max_tokens=S.RAGAS_JUDGE_MAX_TOKENS,
        temperature=0.0,
    )
    judge_emb = create_embeddings()  # same MLX embedder as retrieval
    return LangchainLLMWrapper(judge_llm), LangchainEmbeddingsWrapper(judge_emb)


def _run_ragas(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Score the captured samples with RAGAS. Returns a dict of
    {scenario_id: {metric: score}} plus an ``aggregate`` field with
    macro-averaged scores across the run.

    We score scenario-by-scenario instead of one big ``evaluate()`` call
    because the local judge can be slow (~2 s per yes/no judgement, and
    each metric makes several judgements per sample) and a per-scenario
    progress log is much easier to debug than a single 15-minute
    blocking call."""
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.metrics import (
        Faithfulness,
        AnswerRelevancy,
        LLMContextPrecisionWithoutReference,
    )

    judge_llm, judge_emb = _build_judge()

    faithfulness_m = Faithfulness(llm=judge_llm)
    relevancy_m = AnswerRelevancy(llm=judge_llm, embeddings=judge_emb)
    ctx_precision_m = LLMContextPrecisionWithoutReference(llm=judge_llm)
    metrics = [faithfulness_m, relevancy_m, ctx_precision_m]

    per_scenario: dict[str, dict[str, float | None]] = {}
    for i, s in enumerate(samples, 1):
        sid = s["id"]
        sample = SingleTurnSample(
            user_input=s["user_input"],
            response=s["response"],
            retrieved_contexts=s["retrieved_contexts"],
        )
        print(f"[{i}/{len(samples)}] judging {sid} ...", flush=True)
        dataset = EvaluationDataset(samples=[sample])
        t0 = time.time()
        try:
            result = evaluate(
                dataset=dataset,
                metrics=metrics,
                llm=judge_llm,
                embeddings=judge_emb,
                show_progress=False,
            )
            row = result.to_pandas().iloc[0].to_dict()
            scores = {
                "faithfulness": _safe_float(row.get("faithfulness")),
                "answer_relevancy": _safe_float(row.get("answer_relevancy")),
                "context_precision": _safe_float(
                    row.get("llm_context_precision_without_reference")
                    or row.get("context_precision_without_reference")
                ),
            }
        except Exception as exc:  # noqa: BLE001
            # A single misbehaving scenario must not kill the whole run.
            # Most failures are JSON-parse errors in the judge output —
            # log and move on so the rest of the battery still scores.
            logger.warning("RAGAS failed on %s: %s", sid, exc)
            scores = {"faithfulness": None, "answer_relevancy": None, "context_precision": None}
        elapsed = time.time() - t0
        print(
            f"  -> faith={scores['faithfulness']} "
            f"relev={scores['answer_relevancy']} "
            f"ctxp={scores['context_precision']} ({elapsed:.1f}s)"
        )
        per_scenario[sid] = scores

    aggregate = _macro_average(per_scenario)
    return {"per_scenario": per_scenario, "aggregate": aggregate}


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        # Treat NaN as missing — RAGAS returns NaN when its judge JSON
        # parser fails, which is meaningless noise in aggregate stats.
        if f != f:
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def _macro_average(per_scenario: dict[str, dict[str, float | None]]) -> dict[str, float | None]:
    keys = ["faithfulness", "answer_relevancy", "context_precision"]
    out: dict[str, float | None] = {}
    for k in keys:
        values = [v[k] for v in per_scenario.values() if v.get(k) is not None]
        out[k] = round(sum(values) / len(values), 4) if values else None
        out[f"{k}_n"] = len(values)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="/tmp/hacienda-run/ragas_eval.json",
        help="Where to save the RAGAS results JSON.",
    )
    parser.add_argument(
        "--label",
        default="run",
        help="Run label for downstream A/B diffing.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Run only specific scenario ids (defaults to all 15).",
    )
    parser.add_argument(
        "--samples-only",
        action="store_true",
        help="Capture {query, answer, contexts} but skip the judge step. Use when iterating on the chain.",
    )
    args = parser.parse_args()

    # Match the QA battery's env defaults so a no-arg invocation does
    # the obvious thing on a developer laptop.
    if "FAISS_INDEX_PATH" not in os.environ:
        os.environ["FAISS_INDEX_PATH"] = "./data/faiss-test"
        print("Note: defaulting FAISS_INDEX_PATH=./data/faiss-test")
    os.environ.setdefault("FAISS_TRUSTED_INDEX", "true")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    chain = _build_chain()
    selected = QUERIES if not args.only else [s for s in QUERIES if s["id"] in args.only]
    print(f"Running {len(selected)} scenarios through chain...\n")

    samples: list[dict[str, Any]] = []
    for i, s in enumerate(selected, 1):
        print(f"[{i}/{len(selected)}] {s['id']:35s}", flush=True)
        try:
            samples.append(_run_scenario(chain, s))
        except Exception as exc:  # noqa: BLE001
            print(f"  -> chain ERROR: {exc}")
            samples.append({
                "id": s["id"],
                "user_input": s["q"],
                "response": "(error)",
                "retrieved_contexts": ["(none)"],
                "error": str(exc),
            })

    payload: dict[str, Any] = {"label": args.label, "samples": samples}

    if not args.samples_only:
        print("\nScoring with local MLX judge...")
        payload["ragas"] = _run_ragas(samples)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out} ({out.stat().st_size} bytes)")

    if "ragas" in payload:
        agg = payload["ragas"]["aggregate"]
        print(
            f"\nAggregate ({args.label}): "
            f"faith={agg.get('faithfulness')} "
            f"relev={agg.get('answer_relevancy')} "
            f"ctxp={agg.get('context_precision')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
