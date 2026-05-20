import json
from pathlib import Path


def test_critical_cases_cover_tramites_and_years() -> None:
    path = Path("eval_data/critical_cases/tramite_year_critical_cases.jsonl")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert len(rows) >= 8
    tramites = {r["tramite"] for r in rows}
    assert {"declaracion_irpf", "iva", "autonomo", "generic_tributary", "unknown"}.issubset(tramites)

    years = {r["fiscal_year"] for r in rows}
    assert 2024 in years
    assert 2025 in years
    assert 2026 in years
