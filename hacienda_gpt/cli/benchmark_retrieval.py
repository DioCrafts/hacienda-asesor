from __future__ import annotations

import json
from pathlib import Path

import click
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from hacienda_gpt.llm.chain import _create_retriever
from hacienda_gpt.settings import OPENAI_MODEL, OPENAI_TEMPERATURE
from hacienda_gpt.utils import get_openai_api_key


BENCH_QUESTIONS = [
    {"query": "¿Qué norma aplica al IRPF en 2025?", "expected": ["normativa", "ley"]},
    {"query": "Explícame de forma sencilla cómo presentar la renta", "expected": ["manual", "guia"]},
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
@click.option("--fiscal-year", default=2025, type=int)
@click.option("--output", default="./retrieval_benchmark.json")
def cli(fiscal_year: int, output: str) -> None:
    key = get_openai_api_key()
    llm = ChatOpenAI(temperature=OPENAI_TEMPERATURE, model=OPENAI_MODEL, api_key=key)
    embeddings = OpenAIEmbeddings(api_key=key)

    decision = _create_retriever(embeddings, llm, profile_name="decision", fiscal_year=fiscal_year)
    explain = _create_retriever(embeddings, llm, profile_name="explain", fiscal_year=fiscal_year)

    rows = []
    for item in BENCH_QUESTIONS:
        query = item["query"]
        expected = item["expected"]
        docs_decision = decision.get_relevant_documents(query)
        docs_explain = explain.get_relevant_documents(query)
        rows.append(
            {
                "query": query,
                "decision_score": round(_score_docs(docs_decision, expected), 4),
                "explain_score": round(_score_docs(docs_explain, expected), 4),
            }
        )

    result = {
        "fiscal_year": fiscal_year,
        "rows": rows,
        "avg_decision_score": round(sum(r["decision_score"] for r in rows) / len(rows), 4),
        "avg_explain_score": round(sum(r["explain_score"] for r in rows) / len(rows), 4),
    }
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
