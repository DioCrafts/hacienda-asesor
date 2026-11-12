"""Read the user's own tax documents and propose profile facts from them.

A withholding certificate, a payslip or the AEAT "datos fiscales" already hold
the figures the calculator needs. This module extracts the text, asks the LLM
to locate those figures, and returns them as *proposals*: nothing is written to
the case until the user confirms.

The safeguard against an invented number is mechanical, not a prompt: every
proposed fact must come with the literal passage it was read from, and a
proposal whose passage does not appear in the document is discarded.

Scanned PDFs and photos have no text to check against, so the model reads the
pages itself and the safeguard changes: the document is read TWICE,
independently, and only the facts on which both readings agree are proposed —
flagged as read from an image, for the user to check against the paper.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import Attachment
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, _coerce_value
from hacienda_gpt.tax.profile import PROFILE_FACT_TYPES

logger = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 60
# Scanned documents are read by the model page by page: fewer pages allowed.
MAX_SCAN_PAGES = 20
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
# Facts a document can establish: the fiscal profile plus the fiscal year.
DOCUMENT_FACTS: tuple[str, ...] = (*PROFILE_FACT_TYPES, "periodo_fiscal")


class DocumentoNoLegibleError(ValueError):
    """The upload cannot be read (bad encoding, unsupported format, too large…)."""


class DocumentoEscaneadoError(DocumentoNoLegibleError):
    """A PDF with no text layer: it must be read as images."""

    def __init__(self, message: str, *, paginas: int) -> None:
        super().__init__(message)
        self.paginas = paginas


class HechoPropuesto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | float | bool
    # The literal passage of the document the value was read from.
    evidencia: str
    # "texto": the passage was found literally in the document. "imagen": read by
    # the model from a scan or photo and confirmed by a second, independent reading.
    origen: Literal["texto", "imagen"] = "texto"


class AnalisisDocumento(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    tipo_documento: str
    paginas: int
    caracteres: int
    lectura: Literal["texto", "imagen"] = "texto"
    hechos_propuestos: list[HechoPropuesto] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)


def decode_upload(content_base64: str) -> bytes:
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DocumentoNoLegibleError("El contenido del documento no es base64 válido.") from exc
    if not data:
        raise DocumentoNoLegibleError("El documento está vacío.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentoNoLegibleError("El documento supera el tamaño máximo de 10 MB.")
    return data


def extract_text(data: bytes, filename: str) -> tuple[str, int]:
    """Return (text, pages). A PDF without a text layer raises ``DocumentoEscaneadoError``."""
    if data[:5] == b"%PDF-":
        try:
            import pypdfium2
        except ImportError as exc:  # pragma: no cover - ships with docling
            raise DocumentoNoLegibleError("No está disponible el lector de PDF en este servidor.") from exc
        try:
            pdf = pypdfium2.PdfDocument(data)
        except Exception as exc:
            raise DocumentoNoLegibleError(
                "No se pudo abrir el PDF (¿está dañado o protegido con contraseña?)."
            ) from exc
        try:
            pages = len(pdf)
            if pages > MAX_PAGES:
                raise DocumentoNoLegibleError(f"El PDF tiene {pages} páginas; el máximo es {MAX_PAGES}.")
            parts: list[str] = []
            for index in range(pages):
                textpage = pdf[index].get_textpage()
                parts.append(textpage.get_text_range())
        finally:
            pdf.close()
        text = "\n".join(parts).strip()
        if len(text) < 40:
            raise DocumentoEscaneadoError("El PDF no contiene texto seleccionable (parece escaneado).", paginas=pages)
        return text, pages
    if b"\x00" in data[:2000]:
        raise DocumentoNoLegibleError(
            f"Formato no admitido ({filename}). Sube un PDF, una imagen (PNG, JPEG, WebP) o un archivo de texto."
        )
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding).strip(), 1
        except UnicodeDecodeError:
            continue
    raise DocumentoNoLegibleError("No se pudo leer el archivo como texto.")  # pragma: no cover


SYSTEM_PROMPT = """\
Lees un documento fiscal de un contribuyente español (certificado de retenciones, nómina, datos fiscales de la \
AEAT, certificado de aportaciones a un plan de pensiones, certificado de donativos, extracto de un bróker…) y \
localizas en él los datos de su perfil fiscal. El resultado se le mostrará al usuario para que lo confirme antes de \
guardarlo, y cada dato se comprobará contra el texto del documento.

Para cada dato que el documento diga de forma expresa, devuelve:
- `name`: el campo del perfil al que corresponde.
- `value`: el valor como texto. Los importes, el total ANUAL en euros y solo con dígitos y punto decimal \
("38250.40"); si el documento es de un solo mes (una nómina), no lo anualices: no devuelvas ese importe.
- `evidencia`: el fragmento LITERAL del documento, copiado tal cual y sin resumir, donde aparece ese valor \
(la etiqueta y la cifra, una línea o parte de ella).

Campos y qué buscar:
- rendimiento_trabajo_bruto: retribuciones dinerarias y en especie íntegras del año (suma si hay varias).
- cotizaciones_seguridad_social: gastos deducibles por cotizaciones del trabajador.
- retenciones: retenciones e ingresos a cuenta del IRPF practicados en el año.
- rendimiento_capital_mobiliario, ganancias_patrimoniales, rendimiento_alquiler_neto, rendimiento_actividad_neto.
- aportacion_plan_pensiones (del partícipe), contribucion_empresa_plan_pensiones (del promotor), donativos, \
gastos_guarderia, pagos_vivienda_habitual_pre2013.
- periodo_fiscal: el ejercicio al que se refiere el documento (año de cuatro cifras).
- comunidad_autonoma, estado_civil, edad, num_descendientes y similares solo si constan de forma expresa.

No deduzcas ni calcules nada que no esté escrito, salvo sumar importes del mismo concepto (cita entonces uno de \
ellos como evidencia). Si un dato no aparece, no lo incluyas. En `tipo_documento` describe en pocas palabras qué \
es el documento.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tipo_documento": {"type": "string"},
        "hechos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "enum": list(DOCUMENT_FACTS)},
                    "value": {"type": "string"},
                    "evidencia": {"type": "string"},
                },
                "required": ["name", "value", "evidencia"],
            },
        },
    },
    "required": ["tipo_documento", "hechos"],
}


SYSTEM_PROMPT_IMAGEN = SYSTEM_PROMPT + """
El documento te llega como imagen (escaneado o fotografiado): léelo tú. En `transcripcion` copia TODO el texto \
legible, tal cual, línea a línea, sin corregir ni completar nada. La `evidencia` de cada dato debe ser un fragmento \
literal de tu transcripción. Si una cifra no se lee con claridad (borrosa, cortada, tapada), NO la incluyas como dato.
"""

SCHEMA_IMAGEN: dict[str, Any] = {
    **SCHEMA,
    "properties": {"transcripcion": {"type": "string"}, **SCHEMA["properties"]},
    "required": ["transcripcion", *SCHEMA["required"]],
}


def tipo_de_imagen(data: bytes) -> str | None:
    """Media type of an image upload, by its signature (the file name is not trusted)."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize(text: str) -> str:
    """Case-, accent- and whitespace-insensitive form for passage matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", "", stripped).lower()


def _hechos_validos(raw: dict[str, Any], haystack: str) -> tuple[dict[str, HechoPropuesto], list[str]]:
    """Keep the facts whose passage is in ``haystack`` (normalised); return them and the discarded names."""
    proposed: dict[str, HechoPropuesto] = {}
    discarded: list[str] = []
    for item in raw.get("hechos") or []:
        if not isinstance(item, dict):
            continue
        name, value, evidence = item.get("name"), item.get("value"), item.get("evidencia")
        expected = EXPECTED_FACT_TYPE.get(name) if isinstance(name, str) else None
        if not isinstance(name, str) or expected is None or name not in DOCUMENT_FACTS:
            continue
        if not isinstance(evidence, str) or len(_normalize(evidence)) < 4 or _normalize(evidence) not in haystack:
            # The cited passage is not in the document: the value cannot be trusted.
            discarded.append(name)
            continue
        coerced = _coerce_value(
            expected=expected,
            value_string=value if isinstance(value, str) else None,
            value_number=value if isinstance(value, int | float) and not isinstance(value, bool) else None,
            value_boolean=value if isinstance(value, bool) else None,
        )
        if coerced is None:
            discarded.append(name)
            continue
        proposed.setdefault(name, HechoPropuesto(name=name, value=coerced, evidencia=" ".join(evidence.split())))
    return proposed, discarded


def _tipo(raw: dict[str, Any]) -> str:
    tipo = raw.get("tipo_documento")
    return tipo.strip() if isinstance(tipo, str) and tipo.strip() else "Documento"


def _transcripcion(raw: dict[str, Any]) -> str:
    value = raw.get("transcripcion")
    return value if isinstance(value, str) else ""


def _leer_imagen(provider: Any, *, filename: str, adjunto: Attachment, paginas: int) -> AnalisisDocumento:
    """Two independent readings of a scan or photo; only what both agree on is proposed."""
    lecturas: list[tuple[dict[str, Any], dict[str, HechoPropuesto], list[str]]] = []
    for _ in range(2):
        raw = provider.complete_json(
            system=SYSTEM_PROMPT_IMAGEN,
            user=f'El documento adjunto se llama "{filename}". Transcríbelo y localiza los datos del perfil fiscal.',
            schema=SCHEMA_IMAGEN,
            schema_name="datos_documento_fiscal_escaneado",
            attachments=[adjunto],
        )
        lecturas.append((raw, *_hechos_validos(raw, _normalize(_transcripcion(raw)))))
    (raw, primera, descartados), (_, segunda, descartados_2) = lecturas

    coinciden = {name: fact for name, fact in primera.items() if name in segunda and segunda[name].value == fact.value}
    discrepan = sorted((set(primera) | set(segunda)) - set(coinciden))
    hechos = [fact.model_copy(update={"origen": "imagen"}) for fact in coinciden.values()]

    avisos = [
        "Documento leído a partir de la imagen (escaneado o fotografiado): los datos no se pueden comprobar contra un "
        "texto, así que revisa cada cifra con el papel antes de guardarla."
    ]
    if discrepan:
        avisos.append(
            "Se han descartado datos en los que dos lecturas independientes del documento no coinciden: "
            + ", ".join(discrepan)
            + ". Introdúcelos a mano."
        )
    sin_pasaje = sorted((set(descartados) | set(descartados_2)) - set(discrepan) - set(coinciden))
    if sin_pasaje:
        avisos.append("Se han descartado datos sin un pasaje legible que los respalde: " + ", ".join(sin_pasaje) + ".")
    if not hechos:
        avisos.append("No se han encontrado datos del perfil fiscal legibles en este documento.")
    return AnalisisDocumento(
        filename=filename,
        tipo_documento=_tipo(raw),
        paginas=paginas,
        caracteres=len(_transcripcion(raw)),
        lectura="imagen",
        hechos_propuestos=hechos,
        avisos=avisos,
    )


def analizar_documento(provider: Any, *, filename: str, data: bytes) -> AnalisisDocumento:
    """Extract proposed facts. Raises ``DocumentoNoLegibleError`` or the provider's error."""
    imagen = tipo_de_imagen(data)
    if imagen is not None:
        return _leer_imagen(provider, filename=filename, adjunto=Attachment(imagen, data, filename), paginas=1)
    try:
        text, pages = extract_text(data, filename)
    except DocumentoEscaneadoError as exc:
        if exc.paginas > MAX_SCAN_PAGES:
            raise DocumentoNoLegibleError(
                f"El PDF está escaneado y tiene {exc.paginas} páginas; el máximo para documentos escaneados es "
                f"{MAX_SCAN_PAGES}."
            ) from exc
        return _leer_imagen(
            provider, filename=filename, adjunto=Attachment("application/pdf", data, filename), paginas=exc.paginas
        )
    raw = provider.complete_json(
        system=SYSTEM_PROMPT,
        user=f'<documento nombre="{filename}">\n{text}\n</documento>',
        schema=SCHEMA,
        schema_name="datos_documento_fiscal",
    )
    proposed, discarded = _hechos_validos(raw, _normalize(text))

    avisos: list[str] = []
    if discarded:
        avisos.append(
            "Se han descartado datos que no se pudieron localizar literalmente en el documento: "
            + ", ".join(sorted(set(discarded)))
            + "."
        )
    if not proposed:
        avisos.append("No se han encontrado datos del perfil fiscal en este documento.")
    return AnalisisDocumento(
        filename=filename,
        tipo_documento=_tipo(raw),
        paginas=pages,
        caracteres=len(text),
        hechos_propuestos=list(proposed.values()),
        avisos=avisos,
    )
