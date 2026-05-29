from __future__ import annotations

import json
from pathlib import Path

import click
from langchain_openai import ChatOpenAI

from hacienda_gpt.llm.chain import _create_retriever
from hacienda_gpt.llm.embeddings import create_embeddings
from hacienda_gpt.settings import OPENAI_MODEL, OPENAI_TEMPERATURE
from hacienda_gpt.utils import get_openai_api_key

BENCH_QUESTIONS: list[tuple[str, list[str]]] = [
    ("¿Qué norma aplica al IRPF en 2025?", ["normativa", "ley"]),
    ("Explícame de forma sencilla cómo presentar la renta", ["manual", "guia"]),
]


def _score_docs(docs, expected_tokens: list[str]) -> float:
    if not docs:
        return 0.0
    score = 0
    for doc in docs:
        corpus = (doc.page_content + " " + json.dumps(doc.metadata, ensure_ascii=False)).lower()
        if any(token in corpus for token in expected_tokens):
            score += 1
    return score / len(docs)


@click.command()
@click.option("--output", default="./retrieval_benchmark.json")
def cli(output: str) -> None:
    key = get_openai_api_key()
    llm = ChatOpenAI(temperature=OPENAI_TEMPERATURE, model=OPENAI_MODEL, api_key=key)
    embeddings = create_embeddings()

    decision = _create_retriever(embeddings, llm, profile_name="decision")
    explain = _create_retriever(embeddings, llm, profile_name="explain")

    rows = []
    decision_scores: list[float] = []
    explain_scores: list[float] = []
    for query, expected in BENCH_QUESTIONS:
        decision_score = round(_score_docs(decision.invoke(query), expected), 4)
        explain_score = round(_score_docs(explain.invoke(query), expected), 4)
        decision_scores.append(decision_score)
        explain_scores.append(explain_score)
        rows.append({"query": query, "decision_score": decision_score, "explain_score": explain_score})

    result = {
        "rows": rows,
        "avg_decision_score": round(sum(decision_scores) / len(decision_scores), 4),
        "avg_explain_score": round(sum(explain_scores) / len(explain_scores), 4),
    }
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
