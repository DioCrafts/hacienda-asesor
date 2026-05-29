from langchain_core.documents import Document

from hacienda_gpt.processor.document_loader import DocumentProcessor


class DummyEmbeddings:
    def embed_documents(self, texts):
        return [[0.0] * 3 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 3


def _processor() -> DocumentProcessor:
    return DocumentProcessor(embeddings=DummyEmbeddings(), content_dir=".", output_dir=".")


def test_enrich_metadata_detects_legal_fields() -> None:
    processor = _processor()
    doc = Document(
        page_content="Ley 35/2006. Vigencia: 01/01/2025. Agencia Tributaria España.",
        metadata={"source": "https://example.org/normativa/ley-irpf"},
    )
    meta = processor._enrich_metadata(doc, section="Ámbito")

    assert meta["document_type"] == "normativa"
    assert meta["normative_document_type"] == "ley"
    assert meta["effective_date"] == "01/01/2025"
    assert meta["scope"] == "nacional"
    assert meta["source_hierarchy"] in {
        "ley",
        "reglamento",
        "acto_administrativo",
        "guia_administrativa",
        "constitucion",
    }


def test_enrich_metadata_tags_year_specific_documents() -> None:
    processor = _processor()
    doc = Document(
        page_content="Manual de la Renta 2024. Campaña IRPF ejercicio 2024.",
        metadata={"source": "https://sede.agenciatributaria.gob.es/manual-renta-2024"},
    )
    meta = processor._enrich_metadata(doc)
    assert meta["fiscal_year"] == 2024


def test_enrich_metadata_leaves_evergreen_norm_untagged() -> None:
    # A bare "2006" inside "Ley 35/2006" must not be mistaken for a fiscal year,
    # so the best-effort retrieval filter keeps returning the law for any year.
    processor = _processor()
    doc = Document(
        page_content="Ley 35/2006, de 28 de noviembre, del IRPF.",
        metadata={"source": "https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764"},
    )
    meta = processor._enrich_metadata(doc)
    assert meta["fiscal_year"] is None


def test_semantic_split_preserves_legal_context_header() -> None:
    processor = _processor()
    html_like = Document(
        page_content="""
        <h1>Normativa IRPF</h1>
        <p>Contenido legal relevante.</p>
        <h2>Vigencia</h2>
        <p>En vigor desde 01/01/2025</p>
        """,
        metadata={"source": "https://example.org/normativa/test.html"},
    )

    chunks = processor._semantic_split(html_like)
    assert len(chunks) >= 1
    assert any("[LEGAL_SECTION_CONTEXT]" in chunk.page_content for chunk in chunks)
    assert any("legal_context_header" in chunk.metadata for chunk in chunks)
