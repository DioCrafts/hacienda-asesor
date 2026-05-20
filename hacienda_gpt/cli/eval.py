from __future__ import annotations

import json
import re
from statistics import mean

import click
from langchain_core.messages import AIMessage, HumanMessage

from hacienda_gpt.llm.chain import create_openai_chain
from hacienda_gpt.utils import get_openai_api_key

GOLDEN_SET: list[dict[str, str | list[str]]] = [
    {
        "question": "¿Dónde puedo consultar el calendario del contribuyente?",
        "must_include": ["calendario", "contribuyente"],
    },
    {
        "question": "¿Qué es el IRPF en la Agencia Tributaria?",
        "must_include": ["irpf", "impuesto", "renta"],
    },
    {
        "question": "¿Dónde encuentro información de aplazamientos y fraccionamientos?",
        "must_include": ["aplaz", "fraccion", "deuda"],
    },
]

UNCERTAINTY_PATTERNS = [
    r"hmm, no estoy seguro",
    r"no estoy seguro",
    r"no dispongo de información",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _keyword_coverage_score(answer: str, expected_terms: list[str]) -> float:
    normalized = _normalize(answer)
    matched = sum(1 for term in expected_terms if term.lower() in normalized)
    return matched / len(expected_terms)


def _citation_score(answer: str) -> float:
    normalized = _normalize(answer)
    has_url = "http://" in normalized or "https://" in normalized
    has_source_mention = any(token in normalized for token in ["fuente", "según", "agenciatributaria.gob.es"])
    return 1.0 if has_url or has_source_mention else 0.0


def _grounding_confidence_score(answer: str) -> float:
    normalized = _normalize(answer)
    has_uncertainty = any(re.search(pattern, normalized) for pattern in UNCERTAINTY_PATTERNS)
    return 0.0 if has_uncertainty else 1.0


def _score_answer(answer: str, expected_terms: list[str]) -> dict[str, float]:
    keyword_score = _keyword_coverage_score(answer, expected_terms)
    citation_score = _citation_score(answer)
    grounding_score = _grounding_confidence_score(answer)

    # Weighted score to move beyond binary must_include heuristics.
    final_score = 0.6 * keyword_score + 0.25 * citation_score + 0.15 * grounding_score

    return {
        "keyword_score": round(keyword_score, 4),
        "citation_score": round(citation_score, 4),
        "grounding_score": round(grounding_score, 4),
        "score": round(final_score, 4),
    }


@click.command()
@click.option("--output", default="./eval_results.json", help="Output json file")
def cli(output: str) -> None:
    chain = create_openai_chain(openai_api_key=get_openai_api_key())
    history: list[HumanMessage | AIMessage] = []
    results = []

    for item in GOLDEN_SET:
        result = chain.invoke({"input": item["question"], "chat_history": history})
        answer = result["answer"]
        metrics = _score_answer(answer, item["must_include"])
        results.append(
            {
                "question": item["question"],
                "answer": answer,
                "expected_terms": item["must_include"],
                **metrics,
            }
        )
        history.extend([HumanMessage(content=str(item["question"])), AIMessage(content=answer)])

    summary = {
        "avg_score": round(mean([entry["score"] for entry in results]), 4),
        "avg_keyword_score": round(mean([entry["keyword_score"] for entry in results]), 4),
        "avg_citation_score": round(mean([entry["citation_score"] for entry in results]), 4),
        "avg_grounding_score": round(mean([entry["grounding_score"] for entry in results]), 4),
        "total": len(results),
        "results": results,
    }
    with open(output, "w", encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)

    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
