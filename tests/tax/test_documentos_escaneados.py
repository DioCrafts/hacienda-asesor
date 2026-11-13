"""Scanned PDFs and photos: the model reads them, twice, and only agreement survives."""

from __future__ import annotations

import io
from typing import Any

import pytest

from hacienda_gpt.agent.providers.anthropic_provider import AnthropicProvider
from hacienda_gpt.agent.providers.base import Attachment
from hacienda_gpt.agent.providers.openai_provider import OpenAIProvider
from hacienda_gpt.tax.documentos import DocumentoNoLegibleError, analizar_documento, tipo_de_imagen

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TRANSCRIPCION = "CERTIFICADO DE RETENCIONES\nEjercicio 2026\nRetribuciones íntegras: 38.250,40\nRetenciones: 6.502,57"


class _Lector:
    name, model = "fake", "fake-1"

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses, self.requests = list(responses), []

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _lectura(*facts: tuple[str, str, str], transcripcion: str = TRANSCRIPCION) -> dict[str, Any]:
    return {
        "transcripcion": transcripcion,
        "tipo_documento": "Certificado de retenciones",
        "hechos": [{"name": n, "value": v, "evidencia": e} for n, v, e in facts],
    }


def _pdf_sin_texto(paginas: int = 1) -> bytes:
    pdfium = pytest.importorskip("pypdfium2")
    pdf = pdfium.PdfDocument.new()
    for _ in range(paginas):
        pdf.new_page(595, 842)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_image_types_are_detected_by_signature() -> None:
    assert tipo_de_imagen(PNG) == "image/png"
    assert tipo_de_imagen(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert tipo_de_imagen(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert tipo_de_imagen(b"%PDF-1.7") is None


def test_scanned_pdf_is_sent_to_the_model_and_read_twice() -> None:
    bruto = ("rendimiento_trabajo_bruto", "38250.40", "Retribuciones íntegras: 38.250,40")
    retenciones = ("retenciones", "6502.57", "Retenciones: 6.502,57")
    provider = _Lector(_lectura(bruto, retenciones), _lectura(bruto, retenciones))
    data = _pdf_sin_texto()

    analysis = analizar_documento(provider, filename="escaneado.pdf", data=data)

    assert len(provider.requests) == 2
    for request in provider.requests:
        (adjunto,) = request["attachments"]
        assert adjunto.media_type == "application/pdf" and adjunto.data == data
        assert "transcripcion" in request["schema"]["required"]
    assert analysis.lectura == "imagen" and analysis.paginas == 1
    assert {f.name: (f.value, f.origen) for f in analysis.hechos_propuestos} == {
        "rendimiento_trabajo_bruto": (38250.40, "imagen"),
        "retenciones": (6502.57, "imagen"),
    }
    assert "revisa cada cifra" in analysis.avisos[0]


def test_facts_the_two_readings_disagree_on_are_dropped() -> None:
    bruto = ("rendimiento_trabajo_bruto", "38250.40", "Retribuciones íntegras: 38.250,40")
    provider = _Lector(
        _lectura(bruto, ("retenciones", "6502.57", "Retenciones: 6.502,57")),
        # The second reading sees an 8 where the first saw a 6, and finds one more fact.
        _lectura(
            bruto,
            ("retenciones", "8502.57", "Retenciones: 8.502,57"),
            ("periodo_fiscal", "2026", "Ejercicio 2026"),
            transcripcion=TRANSCRIPCION.replace("6.502,57", "8.502,57"),
        ),
    )

    analysis = analizar_documento(provider, filename="foto.png", data=PNG)

    assert [f.name for f in analysis.hechos_propuestos] == ["rendimiento_trabajo_bruto"]
    assert provider.requests[0]["attachments"][0].media_type == "image/png"
    discrepancias = next(a for a in analysis.avisos if "no coinciden" in a)
    assert "retenciones" in discrepancias and "periodo_fiscal" in discrepancias


def test_evidence_must_be_in_the_transcription() -> None:
    inventado = ("donativos", "900", "Donativos: 900,00")  # not in what the model itself transcribed
    provider = _Lector(_lectura(inventado), _lectura(inventado))

    analysis = analizar_documento(provider, filename="foto.png", data=PNG)

    assert analysis.hechos_propuestos == []
    assert any("sin un pasaje legible" in a and "donativos" in a for a in analysis.avisos)


def test_long_scans_are_rejected() -> None:
    with pytest.raises(DocumentoNoLegibleError, match="máximo para documentos escaneados"):
        analizar_documento(_Lector(), filename="largo.pdf", data=_pdf_sin_texto(21))


class _Recorder:
    def __init__(self, response: Any) -> None:
        self.response, self.calls = response, []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


def test_anthropic_sends_pdfs_as_document_blocks_before_the_text() -> None:
    class _Block:
        type, text = "text", '{"ok": true}'

    class _Response:
        stop_reason, content = "end_turn", [_Block()]

    messages = _Recorder(_Response())
    client = type("C", (), {"messages": messages, "beta": type("B", (), {"messages": messages})()})()
    provider = AnthropicProvider(model="m", client=client)

    provider.complete_json(
        system="S",
        user="U",
        schema={"type": "object"},
        schema_name="x",
        attachments=[Attachment("application/pdf", b"%PDF-1.7 x", "a.pdf"), Attachment("image/png", PNG, "b.png")],
    )

    content = messages.calls[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["document", "image", "text"]
    assert content[0]["source"] == {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjcgeA=="}
    assert content[2]["text"] == "U"


def test_openai_sends_pdf_pages_as_images() -> None:
    message = type("M", (), {"content": '{"ok": true}'})()
    response = type("R", (), {"choices": [type("Ch", (), {"message": message})()]})()
    completions = _Recorder(response)
    client = type("C", (), {"chat": type("Chat", (), {"completions": completions})()})()

    OpenAIProvider(model="m", client=client).complete_json(
        system="S",
        user="U",
        schema={"type": "object"},
        schema_name="x",
        attachments=[Attachment("application/pdf", _pdf_sin_texto(2), "a.pdf")],
    )

    content = completions.calls[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "U"}
    assert len(content) == 3
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in content[1:])
