from pathlib import Path


def test_intent_taxonomy_doc_exists_with_required_sections() -> None:
    path = Path("docs/domain/intent-taxonomy.md")
    assert path.exists()
    text = path.read_text(encoding="utf-8")

    required_markers = [
        "declaracion_irpf",
        "iva",
        "autonomo",
        "generic_tributary",
        "unknown",
        "Facts requeridos",
        "Facts críticos bloqueantes",
        "Ejemplos de lenguaje natural",
    ]
    for marker in required_markers:
        assert marker in text
