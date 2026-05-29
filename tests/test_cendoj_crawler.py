from __future__ import annotations

from pathlib import Path

from hacienda_gpt.crawler.cendoj import parse_cendoj_document

FIXTURES = Path(__file__).parent / "fixtures" / "cendoj"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_extracts_roj_and_ecli_for_tributary_sentence() -> None:
    metadata = parse_cendoj_document(
        _read("sentencia_tributaria.html"),
        source_url="https://www.poderjudicial.es/search/AN/openCDocument/foo/2024",
    )

    assert metadata["roj"] == "STS 1234/2024"
    assert metadata["ecli"] == "ES:TS:2024:1234"
    assert metadata["fecha_resolucion"] == "2024-03-15"
    assert metadata["ponente"].startswith("Excma")
    assert metadata["tribunal"] == "Tribunal Supremo"
    assert metadata["sala"] is not None
    assert "Tercera" in metadata["sala"] or "Contencioso" in metadata["sala"]
    assert metadata["source_authority"] == "TS_SALA_TERCERA"
    assert metadata["source_hierarchy_rank"] == 10
    assert metadata["is_tributario"] is True
    assert "IS" in metadata["tributos_detectados"]


def test_parse_marks_non_tributary_sentence_correctly() -> None:
    metadata = parse_cendoj_document(_read("sentencia_no_tributaria.html"))

    assert metadata["roj"] == "STS 5678/2024"
    assert metadata["is_tributario"] is False
    # Non-tributary tax-keyword catalog should be empty.
    assert metadata["tributos_detectados"] == []


def test_classifier_promotes_when_two_weak_keywords_appear() -> None:
    html = "<html><body><p>Cuestión tributaria sobre liquidación de un tributo municipal.</p></body></html>"
    metadata = parse_cendoj_document(html)
    assert metadata["is_tributario"] is True


def test_classifier_does_not_promote_on_single_weak_keyword() -> None:
    html = "<html><body><p>Cuestión sobre régimen fiscal de empleados públicos.</p></body></html>"
    metadata = parse_cendoj_document(html)
    assert metadata["is_tributario"] is False


def test_parse_detects_all_main_tributos() -> None:
    html = (
        "<html><body>"
        "ROJ: STS 9999/2024. "
        "Análisis conjunto de IRPF, IVA, IS, ITP, AJD e ISD aplicable al supuesto."
        "</body></html>"
    )
    metadata = parse_cendoj_document(html)
    detected = set(metadata["tributos_detectados"])
    assert {"IRPF", "IVA", "IS", "ITP_AJD", "ISD"}.issubset(detected)


def test_parse_handles_missing_metadata_gracefully() -> None:
    metadata = parse_cendoj_document("<html><body>contenido vacío</body></html>")
    assert metadata["roj"] is None
    assert metadata["ecli"] is None
    assert metadata["fecha_resolucion"] is None
    assert metadata["is_tributario"] is False
