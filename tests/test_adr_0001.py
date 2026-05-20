from pathlib import Path


def test_adr_0001_exists_and_has_required_sections() -> None:
    adr_path = Path("docs/adr/0001-decision-assistant-architecture.md")
    assert adr_path.exists()

    content = adr_path.read_text(encoding="utf-8")

    required_markers = [
        "## 1) Contexto actual",
        "## 2) Problemas detectados en la arquitectura vigente",
        "## 3) Decisión: arquitectura objetivo por capas",
        "UI/API",
        "Orquestación",
        "NLU",
        "Reglas",
        "Retrieval",
        "Planner",
        "Explainability",
        "Storage",
        "## 5) Trade-offs",
        "## 6) Riesgos y mitigaciones",
        "## 7) Plan incremental de migración (sin detener funcionalidad actual)",
        "```mermaid",
    ]

    for marker in required_markers:
        assert marker in content
