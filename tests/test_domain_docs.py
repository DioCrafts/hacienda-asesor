from pathlib import Path


def test_glossary_contains_required_terms() -> None:
    glossary = Path("docs/domain/glossary.md")
    assert glossary.exists()
    text = glossary.read_text(encoding="utf-8")

    required_terms = [
        "Hecho fiscal",
        "Obligación candidata",
        "Confianza",
        "Evidencia",
        "Acción",
        "Riesgo",
    ]
    for term in required_terms:
        assert term in text


def test_contracts_contains_required_entities_and_versioning_policy() -> None:
    contracts = Path("docs/domain/contracts.md")
    assert contracts.exists()
    text = contracts.read_text(encoding="utf-8")

    required_markers = [
        "`CaseState`",
        "`ObligationCandidate`",
        "`ActionPlan`",
        "`EvidenceRef`",
        "`QuestionPrompt`",
        "## Política de versionado de esquemas",
        '"schema_version": "1.0.0"',
    ]
    for marker in required_markers:
        assert marker in text
