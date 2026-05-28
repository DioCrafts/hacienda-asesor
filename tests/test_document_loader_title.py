"""Title extraction by source family — BOE, TEAC, AEAT fallback.

The grounding gate decides whether the LLM can claim it cited a
document; titles are part of the chunk metadata it sees. When every
BOE chunk surfaced as "Agencia Estatal Boletín Oficial del Estado"
and every TEAC chunk as "DYCTEA", the model hedged even with
relevant citations. These tests pin the cleanup heuristics so the
regression never lands again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.documents import Document

from hacienda_gpt.processor import document_loader
from hacienda_gpt.processor.document_loader import (
    DocumentProcessor,
    _read_source_html,
    _title_from_source,
)


class _DummyEmbeddings:
    def embed_documents(self, texts):
        return [[0.0] * 3 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 3


@pytest.fixture(autouse=True)
def _clear_title_cache():
    _read_source_html.cache_clear()
    yield
    _read_source_html.cache_clear()


def _processor() -> DocumentProcessor:
    return DocumentProcessor(embeddings=_DummyEmbeddings(), content_dir=".", output_dir=".")


def _write(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def test_boe_title_strips_id_prefix(tmp_path: Path) -> None:
    path = tmp_path / "boe-modelo-720" / "BOE-A-2013-954.html"
    _write(
        path,
        """<html><head><title>BOE-A-2013-954 Orden HAP/72/2013, de 30 de enero, por la que se aprueba el modelo 720, declaración informativa sobre bienes y derechos situados en el extranjero, a que se refiere la disposición adicional decimoctava de la Ley 58/2003, de 17 de diciembre, General Tributaria y se determinan el lugar, forma, plazo y el procedimiento para su presentación.</title></head><body>Agencia Estatal Boletín Oficial del Estado</body></html>""",
    )

    title = _title_from_source(str(path))
    assert title is not None
    assert title.startswith("Orden HAP/72/2013")
    assert "BOE-A-2013-954" not in title


def test_boe_title_for_stc_keeps_pleno_label(tmp_path: Path) -> None:
    path = tmp_path / "boe-jurisprudencia-tc" / "BOE-A-2021-19511.html"
    _write(
        path,
        "<html><head><title>BOE-A-2021-19511 Pleno. Sentencia 182/2021, de 26 de octubre de 2021. Cuestión de inconstitucionalidad 4433-2020...</title></head><body>x</body></html>",
    )

    title = _title_from_source(str(path))
    assert title is not None
    assert title.startswith("Pleno. Sentencia 182/2021")


def test_teac_title_uses_asunto_when_available(tmp_path: Path) -> None:
    path = tmp_path / "teac" / "00_00636_2022_00_0_1.html"
    _write(
        path,
        """<html><head><title>DYCTEA</title><body>
        Criterio 1 de 1 de la resolución: 00/00636/2022/00/00
        Calificación: Doctrina
        Unidad resolutoria: TEAC
        Fecha de la resolución: 19/11/2024
        Asunto: IRPF. Régimen fiscal especial de trabajadores desplazados a territorio español. Condición de administrador.
        Referencias normativas: Ley 35/2006
        </body></html>""",
    )

    title = _title_from_source(str(path))
    assert title is not None
    assert title.startswith("TEAC — IRPF")
    assert "trabajadores desplazados" in title


def test_teac_title_falls_back_to_criterio_when_asunto_missing(tmp_path: Path) -> None:
    path = tmp_path / "teac" / "weird.html"
    _write(
        path,
        "<html><head><title>DYCTEA</title></head><body>Criterio 1 de 1 de la resolución: 54/00218/2024/00/00</body></html>",
    )

    title = _title_from_source(str(path))
    assert title == "TEAC criterio 54/00218/2024/00/00"


def test_default_source_uses_title_tag(tmp_path: Path) -> None:
    path = tmp_path / "aeat" / "some.html"
    _write(
        path,
        "<html><head><title>Soy autónomo, ¿qué obligaciones tengo con la AEAT?</title></head><body>hola</body></html>",
    )

    title = _title_from_source(str(path))
    assert title == "Soy autónomo, ¿qué obligaciones tengo con la AEAT?"


def test_extract_title_falls_back_to_first_line_when_no_source(tmp_path: Path) -> None:
    processor = _processor()
    text = "  \n\nPrimera línea útil\nSegunda línea ignorada"
    # No source path → must still produce a non-trivial title from the body.
    assert processor._extract_title(text, source_path="") == "Primera línea útil"


def test_enrich_metadata_uses_boe_title(tmp_path: Path) -> None:
    path = tmp_path / "boe-modelo-720" / "BOE-A-2012-13416.html"
    _write(
        path,
        "<html><head><title>BOE-A-2012-13416 Ley 7/2012, de 29 de octubre, de modificación de la normativa tributaria.</title></head><body>Agencia Estatal Boletín Oficial del Estado</body></html>",
    )
    processor = _processor()
    doc = Document(
        page_content="Agencia Estatal Boletín Oficial del Estado\nIr a contenido",
        metadata={"source": str(path)},
    )
    meta = processor._enrich_metadata(doc)
    assert meta["title"].startswith("Ley 7/2012")
    # The BOE locator stays in source_url so the UI can deep-link.
    assert meta["source_url"] == str(path)
