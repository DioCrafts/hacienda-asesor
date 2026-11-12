from pathlib import Path

from click.testing import CliRunner
from langchain_core.documents import Document

from hacienda_gpt.agent.search import SearchFilters
from hacienda_gpt.evals.agent_eval import (
    CaseResult,
    ExcludingSearcher,
    GoldenCase,
    SystemAnswer,
    build_case,
    evaluate_case,
    html_to_text,
    load_golden,
    sample_consulta_files,
    summarize,
    write_jsonl,
)
from .fakes import FakeSearcher, ScriptedProvider, corpus

CONSULTA_HTML = (
    "<html><head><style>p{}</style></head><body><p class='NUM-CONSULTA'>V0058-25</p>"
    "<p>Descripción de hechos: el consultante vendió su vivienda habitual en 2024.</p>"
    "<p>Cuestión planteada: plazo para reinvertir.</p>" + "<p>Contestación: dos años. </p>" * 30 + "</body></html>"
)
CASE = GoldenCase(case_id="V0058-25", question="Vendí mi vivienda, ¿plazo?", expected_answer="Dos años.")


def _usable(**overrides) -> dict:
    return {
        "apta": True,
        "motivo_descarte": "",
        "pregunta": "Vendí mi vivienda habitual en 2024, ¿qué plazo tengo para reinvertir?",
        "respuesta_esperada": "Dos años desde la transmisión.",
        "normativa": ["artículo 38 Ley 35/2006", " "],
        "tributo": "IRPF",
        "fecha": "2025-01-20",
        **overrides,
    }


def test_html_to_text_drops_markup_and_styles() -> None:
    text = html_to_text(CONSULTA_HTML)
    assert "V0058-25" in text and "Cuestión planteada: plazo para reinvertir." in text
    assert "p{}" not in text and "<p" not in text


def test_sample_is_deterministic_and_filters_by_year(tmp_path: Path) -> None:
    for name in ("2025/V0058-25.html", "2019/V1000-19.html", "1998/V0001-98.html", "2025/_crawl_summary.html"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    recent = sample_consulta_files(tmp_path, sample=10, since_year=2019, seed=0)
    assert sorted(p.name for p in recent) == ["V0058-25.html", "V1000-19.html"]
    assert len(sample_consulta_files(tmp_path, sample=10, since_year=None, seed=0)) == 3
    assert sample_consulta_files(tmp_path, sample=1, since_year=None, seed=7) == sample_consulta_files(
        tmp_path, sample=1, since_year=None, seed=7
    )


def test_build_case_keeps_usable_consultas_and_discards_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "V0058-25.html"
    path.write_text(CONSULTA_HTML, encoding="utf-8")

    provider = ScriptedProvider([], [_usable()])
    case = build_case(provider, path)
    assert case is not None and case.case_id == "V0058-25"
    assert case.legal_basis == ["artículo 38 Ley 35/2006"] and case.consulta_date == "2025-01-20"
    assert 'numero="V0058-25"' in provider.json_requests[0]["user"]

    assert build_case(ScriptedProvider([], [_usable(apta=False)]), path) is None
    # A question naming the consulta would hand the system the answer key.
    assert build_case(ScriptedProvider([], [_usable(pregunta="Según la V0058-25, ¿plazo?")]), path) is None

    stub = tmp_path / "V0001-25.html"
    stub.write_text("<p>Error</p>", encoding="utf-8")
    assert build_case(ScriptedProvider([], []), stub) is None  # too short: no LLM call spent

    out = tmp_path / "golden.jsonl"
    write_jsonl(out, [case])
    assert load_golden(out) == [case]


def test_excluding_searcher_hides_the_source_consulta() -> None:
    docs = [*corpus(), Document(page_content="otra", metadata={"title": "Consulta V0099-25", "source": "V0099-25"})]
    searcher = ExcludingSearcher(FakeSearcher(docs), "V0058-25")

    found = searcher.search("reinversión", k=3, filters=SearchFilters())

    assert len(found) == 3
    assert all("V0058-25" not in str(doc.metadata) for doc in found)


def _system(answer: SystemAnswer | Exception):
    def run(case: GoldenCase) -> SystemAnswer:
        if isinstance(answer, Exception):
            raise answer
        return answer

    return run


def test_evaluate_case_judges_answers_and_records_failures() -> None:
    confident = SystemAnswer(answer="Dos años.", mode="verified", confident=True, abstained=False, detail={})
    judge = ScriptedProvider([], [{"veredicto": "correcta", "motivo": "coincide"}])
    result = evaluate_case(_system(confident), judge, CASE)
    assert (result.verdict, result.judge_reason, result.confident) == ("correcta", "coincide", True)
    assert "<referencia_dgt>\nDos años." in judge.json_requests[0]["user"]

    abstained = SystemAnswer(answer="No sé.", mode="abstained", confident=False, abstained=True, detail={})
    silent_judge = ScriptedProvider([], [])
    assert evaluate_case(_system(abstained), silent_judge, CASE).verdict == "sin_respuesta"
    assert silent_judge.json_requests == []  # no judge call spent on an abstention

    crashed = evaluate_case(_system(RuntimeError("sin índice")), ScriptedProvider([], []), CASE)
    assert (crashed.verdict, crashed.error) == ("error", "sin índice")

    bad_judge = ScriptedProvider([], [{"veredicto": "regular", "motivo": ""}])
    assert evaluate_case(_system(confident), bad_judge, CASE).verdict == "error"


def test_summary_highlights_confident_errors() -> None:
    def row(verdict: str, confident: bool, mode: str = "verified") -> CaseResult:
        return CaseResult(
            case_id="x",
            question="q",
            expected_answer="e",
            answer="a",
            mode=mode,
            confident=confident,
            abstained=verdict == "sin_respuesta",
            verdict=verdict,
        )

    summary = summarize(
        [
            row("correcta", True),
            row("correcta", True),
            row("incorrecta", True),
            row("incorrecta", False, "partially_verified"),
            row("sin_respuesta", False, "abstained"),
            row("error", False, "error"),
        ]
    )

    assert (summary["cases"], summary["scored"], summary["errors"]) == (6, 5, 1)
    assert summary["correct_rate"] == 0.4 and summary["no_answer_rate"] == 0.2
    assert summary["correct_when_answered"] == 0.5
    assert summary["confident_answers"] == 3
    assert summary["wrong_when_confident"] == 0.3333
    assert summary["modes"] == {"abstained": 1, "error": 1, "partially_verified": 1, "verified": 3}
    assert summarize([])["correct_rate"] == 0.0


def test_cli_build_requires_consultas(tmp_path: Path) -> None:
    from hacienda_gpt.cli.eval_agent import cli

    result = CliRunner().invoke(cli, ["build", "--dgt-dir", str(tmp_path), "--out", str(tmp_path / "g.jsonl")])
    assert result.exit_code != 0 and "No se encontraron consultas" in result.output
