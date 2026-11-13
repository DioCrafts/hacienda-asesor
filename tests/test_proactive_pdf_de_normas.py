"""The live law reader appends the PDF text of a norm that is not a consolidated text."""

from __future__ import annotations

from typing import Any

import pytest

from hacienda_gpt.cli import proactive

ORDEN = "BOE-A-2025-5407"
PAGINA = (
    '<html><body><div id="textoxslt"><p>Se aprueba el modelo 202.</p></div>'
    f'<a href="/boe/dias/2025/03/19/pdfs/{ORDEN}.pdf">PDF</a></body></html>'
)
LEY = (
    '<html><body><p>Texto consolidado</p><a href="/boe/dias/2014/11/28/pdfs/BOE-A-2014-12328.pdf">PDF</a></body></html>'
)


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    pedidas: list[str] = []

    def get(url: str, *, accept: str, **_: Any) -> bytes | None:
        pedidas.append(url)
        if url.endswith(".pdf"):
            return b"%PDF"
        return (PAGINA if ORDEN in url else LEY).encode()

    monkeypatch.setattr(proactive, "_get", get)
    monkeypatch.setattr(proactive, "_texto_pdf", lambda data: "Base del pago fraccionado ...... 19 <casilla> & más")
    monkeypatch.setattr(proactive.time, "sleep", lambda s: None)
    return pedidas


def test_a_norm_that_is_not_consolidated_carries_the_text_of_its_pdf(descargas: list[str]) -> None:
    pagina = proactive._texto_del_boe()(ORDEN)
    assert pagina is not None and "Se aprueba el modelo 202" in pagina
    # The PDF text is escaped into the page, so the HTML reader keeps it as text.
    assert "Base del pago fraccionado ...... 19 &lt;casilla&gt; &amp; más" in pagina
    assert descargas[-1] == f"https://www.boe.es/boe/dias/2025/03/19/pdfs/{ORDEN}.pdf"


def test_a_consolidated_law_and_a_historical_version_are_read_without_their_pdf(descargas: list[str]) -> None:
    texto_de = proactive._texto_del_boe()
    assert texto_de("BOE-A-2014-12328") == LEY
    assert texto_de(ORDEN, "20250101") == PAGINA
    assert not any(url.endswith(".pdf") for url in descargas)


def test_an_unreadable_pdf_leaves_the_page_alone(monkeypatch: pytest.MonkeyPatch, descargas: list[str]) -> None:
    def roto(data: bytes) -> str:
        raise ValueError("not a PDF")

    monkeypatch.setattr(proactive, "_texto_pdf", roto)
    assert proactive._texto_del_boe()(ORDEN) == PAGINA
