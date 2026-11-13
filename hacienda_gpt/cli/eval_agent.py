"""Build and run the DGT golden-set evaluation.

    # 1) Build the golden set from downloaded consultas (one LLM call each):
    uv run python -m hacienda_gpt.cli.eval_agent build \\
        --dgt-dir data/html/<snapshot>/dgt-vinculantes --out eval_data/dgt_golden.jsonl \\
        --sample 200 --since-year 2022

    # 2) Evaluate the agent (and, for comparison, the legacy /qa chain):
    uv run python -m hacienda_gpt.cli.eval_agent run --dataset eval_data/dgt_golden.jsonl \\
        --system agent --out reports/eval_agent.json
    uv run python -m hacienda_gpt.cli.eval_agent run --dataset eval_data/dgt_golden.jsonl \\
        --system qa --out reports/eval_qa.json

Both commands call paid LLM APIs (unless LLM_PROVIDER=local); each prints how
many calls it is about to make and asks before spending.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from hacienda_gpt.evals.agent_eval import (
    CaseResult,
    ExcludingSearcher,
    GoldenCase,
    SystemAnswer,
    SystemUnderTest,
    build_case,
    evaluate_case,
    load_golden,
    sample_consulta_files,
    write_jsonl,
    write_report,
)


@click.group()
def cli() -> None:
    """Golden-set evaluation from DGT binding consultas."""


@cli.command()
@click.option("--dgt-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--sample", default=200, show_default=True, help="How many consultas to draw.")
@click.option("--since-year", default=2022, show_default=True, help="Only consultas from this year on (0 = all).")
@click.option("--seed", default=0, show_default=True, help="Sampling seed (same seed → same sample).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before calling the LLM.")
def build(dgt_dir: Path, out_path: Path, sample: int, since_year: int, seed: int, yes: bool) -> None:
    """Turn sampled DGT consultas into (question, expected answer) cases."""
    from hacienda_gpt.agent.providers import create_provider

    files = sample_consulta_files(dgt_dir, sample=sample, since_year=since_year or None, seed=seed)
    if not files:
        raise click.ClickException(f"No se encontraron consultas (V####-## .html) bajo {dgt_dir}")
    provider = create_provider()
    if not yes:
        click.confirm(
            f"Se harán {len(files)} llamadas a {provider.name}/{provider.model} (una por consulta). ¿Continuar?",
            abort=True,
        )

    cases: list[GoldenCase] = []
    with click.progressbar(files, label="Generando casos") as bar:
        for path in bar:
            try:
                case = build_case(provider, path)
            except Exception as exc:  # keep going: one bad consulta must not lose the batch
                click.echo(f"\n  {path.name}: error ({exc})", err=True)
                continue
            if case is not None:
                cases.append(case)
                write_jsonl(out_path, list(cases))  # checkpoint after every case

    click.echo(f"{len(cases)} casos aptos de {len(files)} consultas → {out_path}")


def _agent_system(exclude_source: bool) -> SystemUnderTest:
    from hacienda_gpt.agent.agent import AgentMode, TaxAgent, build_agent

    base = build_agent()

    def run(case: GoldenCase) -> SystemAnswer:
        agent = base
        if exclude_source:
            agent = TaxAgent(
                provider=base._provider,
                searcher=ExcludingSearcher(base._searcher, case.case_id),
                verifier=base._verifier,
            )
        result = agent.run(case.question)
        return SystemAnswer(
            answer=result.answer,
            mode=result.mode.value,
            confident=result.mode is AgentMode.VERIFIED,
            abstained=result.mode is AgentMode.ABSTAINED,
            detail={
                "steps": result.steps,
                "revisions": result.revisions,
                "searches": [entry.arguments for entry in result.tool_trace if entry.tool == "buscar_fuentes"],
                "citations": [f"{c.fragment_id} {c.title} ({c.locator})" for c in result.citations],
                "warnings": result.warnings,
                "usage": result.usage,
            },
        )

    return run


def _qa_system() -> SystemUnderTest:
    from hacienda_gpt.llm.chain import answer_with_grounding, create_openai_chain
    from hacienda_gpt.llm.grounding import AnswerMode
    from hacienda_gpt.utils import get_openai_api_key

    chain = create_openai_chain(openai_api_key=get_openai_api_key())

    def run(case: GoldenCase) -> SystemAnswer:
        envelope = answer_with_grounding(chain, query=case.question)
        return SystemAnswer(
            answer=envelope.answer,
            mode=envelope.mode.value,
            confident=envelope.mode is AnswerMode.CITED,
            abstained=envelope.mode is AnswerMode.ABSTAINED,
            detail={"citations": [f"{c.title} ({c.locator})" for c in envelope.citations]},
        )

    return run


@cli.command()
@click.option("--dataset", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--system", "system_name", type=click.Choice(["agent", "qa"]), default="agent", show_default=True)
@click.option("--limit", default=0, show_default=True, help="Evaluate only the first N cases (0 = all).")
@click.option(
    "--exclude-source/--include-source",
    default=True,
    show_default=True,
    help="Hide each case's own consulta from the agent's retrieval (the legacy qa chain cannot do this).",
)
@click.option("--judge-provider", default=None, help="Provider for the judge (default: LLM_PROVIDER).")
@click.option("--judge-model", default=None, help="Model for the judge (default: the provider's).")
@click.option("--label", default=None, help="Free-text label stored in the report.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before calling the LLM.")
def run(
    dataset: Path,
    out_path: Path,
    system_name: str,
    limit: int,
    exclude_source: bool,
    judge_provider: str | None,
    judge_model: str | None,
    label: str | None,
    yes: bool,
) -> None:
    """Run a system over the golden set and judge each answer against the DGT's."""
    from hacienda_gpt.agent.providers import create_provider

    cases = load_golden(dataset)
    if limit:
        cases = cases[:limit]
    if not cases:
        raise click.ClickException(f"{dataset} no contiene casos")
    judge = create_provider(judge_provider, model=judge_model)
    if not yes:
        per_case = "varias llamadas (búsquedas + verificación)" if system_name == "agent" else "varias llamadas"
        click.confirm(
            f"Se evaluarán {len(cases)} casos con '{system_name}': {per_case} por caso más una del juez "
            f"({judge.name}/{judge.model}). ¿Continuar?",
            abort=True,
        )
    if system_name == "qa" and exclude_source:
        click.echo("Aviso: la cadena /qa no puede ocultar la consulta de origen; puede encontrar la respuesta.")

    system = _agent_system(exclude_source) if system_name == "agent" else _qa_system()
    results: list[CaseResult] = []
    report_label = label or system_name
    with click.progressbar(cases, label="Evaluando") as bar:
        for case in bar:
            results.append(evaluate_case(system, judge, case))
            write_report(out_path, label=report_label, results=results)  # checkpoint

    summary = write_report(out_path, label=report_label, results=results)
    click.echo(json.dumps(summary, indent=2, ensure_ascii=False))
    click.echo(f"Informe → {out_path}")


if __name__ == "__main__":
    cli()
