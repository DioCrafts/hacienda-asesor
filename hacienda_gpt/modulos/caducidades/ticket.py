"""Propose a purchase (for its warranty and return dates) from a receipt or invoice.

Same safeguard as the tax document reader (:mod:`hacienda_gpt.tax.documentos`), reused, not copied: every
proposed field comes with the literal passage it was read from, and a field whose passage is not in the
document — or whose value is not in its passage — is discarded. Scans and photos are read twice,
independently, and only the fields both readings agree on are proposed. Nothing is saved: the user confirms
and then the purchase is registered with ``caducidades_registrar_compra``.

Two entry points:

- :func:`verificar_propuesta` — deterministic: checks a proposal (made by the agent's model from a text it
  already has) against that text, and adds what can be read without a model (dates, the TOTAL line).
- :func:`leer_ticket` — for an uploaded file, given an LLM provider (``complete_json``), as
  ``documentos.analizar_documento`` does for tax documents.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.agent.providers.base import Attachment
from hacienda_gpt.tax.documentos import (
    DocumentoEscaneadoError,
    DocumentoNoLegibleError,
    _normalize,
    extract_text,
    tipo_de_imagen,
)

CAMPOS = ("descripcion", "establecimiento", "fecha_compra", "fecha_entrega", "importe")
CampoTicket = Literal["descripcion", "establecimiento", "fecha_compra", "fecha_entrega", "importe"]
MAX_TEXTO = 20_000

_MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_FECHA_NUM = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d)")
_FECHA_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_FECHA_TEXTO = re.compile(r"(?<!\d)(\d{1,2}) de ([a-záéíóú]+) de (\d{4})(?!\d)", re.IGNORECASE)
_IMPORTE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d+\.\d{2})(?![\d])")
_TOTAL = re.compile(r"(?<![a-z])total(?!\s*(?:sin|base|art|unid|uds|items))", re.IGNORECASE)


class CampoPropuesto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campo: CampoTicket
    valor: str
    evidencia: str
    origen: Literal["texto", "imagen"] = "texto"


class PropuestaCompra(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campos: list[CampoPropuesto] = Field(default_factory=list)
    descartados: list[str] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)

    def valor(self, campo: str) -> str | None:
        return next((c.valor for c in self.campos if c.campo == campo), None)


def fechas_en(texto: str) -> list[date]:
    """Every valid calendar date written in ``texto`` (dd/mm/aaaa, dd-mm-aa, aaaa-mm-dd, '3 de marzo de 2026')."""
    encontradas: list[date] = []
    for m in _FECHA_ISO.finditer(texto):
        _anadir(encontradas, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    sin_iso = _FECHA_ISO.sub(" ", texto)
    for m in _FECHA_NUM.finditer(sin_iso):
        year = int(m.group(3))
        _anadir(encontradas, year + 2000 if year < 100 else year, int(m.group(2)), int(m.group(1)))
    for m in _FECHA_TEXTO.finditer(texto):
        mes = _MESES.get(m.group(2).lower())
        if mes:
            _anadir(encontradas, int(m.group(3)), mes, int(m.group(1)))
    return encontradas


def _anadir(lista: list[date], year: int, month: int, day: int) -> None:
    try:
        fecha = date(year, month, day)
    except ValueError:
        return
    if fecha not in lista:
        lista.append(fecha)


def importes_en(texto: str) -> list[Decimal]:
    salida: list[Decimal] = []
    for m in _IMPORTE.finditer(texto):
        literal = m.group(1)
        normal = literal.replace(".", "").replace(",", ".") if "," in literal else literal
        try:
            salida.append(Decimal(normal))
        except InvalidOperation:  # pragma: no cover - the regex only matches numbers
            continue
    return salida


def _valor_en_evidencia(campo: str, valor: str, evidencia: str) -> str | None:
    """The canonical value if ``valor`` is what ``evidencia`` says, else None."""
    if campo in ("fecha_compra", "fecha_entrega"):
        try:
            fecha = date.fromisoformat(valor.strip())
        except ValueError:
            candidatas = fechas_en(valor)
            if len(candidatas) != 1:
                return None
            fecha = candidatas[0]
        return fecha.isoformat() if fecha in fechas_en(evidencia) else None
    if campo == "importe":
        leidos = importes_en(valor) or []
        if not leidos:
            try:
                leidos = [Decimal(valor.strip())]
            except InvalidOperation:
                return None
        importe = leidos[0]
        return str(importe.quantize(Decimal("0.01"))) if importe in importes_en(evidencia) else None
    limpio = " ".join(valor.split())
    if len(_normalize(limpio)) < 2 or _normalize(limpio) not in _normalize(evidencia):
        return None
    return limpio[:120]


def _validar(
    raw_campos: Any, haystack: str, origen: Literal["texto", "imagen"]
) -> tuple[dict[str, CampoPropuesto], list[str]]:
    aceptados: dict[str, CampoPropuesto] = {}
    descartados: list[str] = []
    if not isinstance(raw_campos, list):
        return aceptados, descartados
    for item in raw_campos:
        if not isinstance(item, dict):
            continue
        campo, valor, evidencia = item.get("campo"), item.get("valor"), item.get("evidencia")
        if campo not in CAMPOS:
            continue
        if not isinstance(valor, str | int | float) or isinstance(valor, bool):
            descartados.append(str(campo))
            continue
        if not isinstance(evidencia, str) or len(_normalize(evidencia)) < 4 or _normalize(evidencia) not in haystack:
            descartados.append(str(campo))
            continue
        canonico = _valor_en_evidencia(str(campo), str(valor), evidencia)
        if canonico is None:
            descartados.append(str(campo))
            continue
        aceptados.setdefault(
            str(campo),
            CampoPropuesto(campo=campo, valor=canonico, evidencia=" ".join(evidencia.split()), origen=origen),
        )
    return aceptados, descartados


def extraer_sin_modelo(texto: str) -> dict[str, CampoPropuesto]:
    """What can be read deterministically: the date (when only one is written) and the TOTAL line."""
    campos: dict[str, CampoPropuesto] = {}
    lineas = [" ".join(linea.split()) for linea in texto.splitlines() if linea.strip()]
    con_fecha = [(linea, fechas_en(linea)) for linea in lineas]
    todas = {f for _, fs in con_fecha for f in fs}
    if len(todas) == 1:
        fecha = next(iter(todas))
        linea = next(linea for linea, fs in con_fecha if fecha in fs)
        campos["fecha_compra"] = CampoPropuesto(campo="fecha_compra", valor=fecha.isoformat(), evidencia=linea)
    totales = [linea for linea in lineas if _TOTAL.search(linea) and not re.search(r"subtotal", linea, re.I)]
    for linea in totales:
        importes = importes_en(linea)
        if importes:
            campos["importe"] = CampoPropuesto(campo="importe", valor=str(importes[-1]), evidencia=linea)
            break
    return campos


def verificar_propuesta(texto: str, propuesta: list[dict[str, Any]] | None = None) -> PropuestaCompra:
    """Check ``propuesta`` (items {campo, valor, evidencia}) against ``texto`` and add what reads without a model."""
    if len(texto) > MAX_TEXTO:
        texto = texto[:MAX_TEXTO]
    haystack = _normalize(texto)
    aceptados, descartados = _validar(propuesta or [], haystack, "texto")
    automaticos = extraer_sin_modelo(texto)
    avisos: list[str] = []
    for campo, auto in automaticos.items():
        propio = aceptados.get(campo)
        if propio is None:
            aceptados[campo] = auto
        elif propio.valor != auto.valor and campo == "fecha_compra":
            avisos.append(
                f"La fecha propuesta ({propio.valor}) no es la única fecha del documento ({auto.valor}): compruébala."
            )
    if len(set(fechas_en(texto))) > 1 and "fecha_compra" not in aceptados:
        avisos.append("El documento tiene varias fechas: indica cuál es la de compra (y, si la hay, la del albarán).")
    if "descripcion" not in aceptados:
        avisos.append("Falta qué se compró (descripción): indícalo al registrar la compra.")
    if descartados:
        avisos.append(
            "Se han descartado datos que no se pudieron localizar literalmente en el documento: "
            + ", ".join(sorted(set(descartados)))
            + "."
        )
    orden = {campo: i for i, campo in enumerate(CAMPOS)}
    return PropuestaCompra(
        campos=sorted(aceptados.values(), key=lambda c: orden[c.campo]),
        descartados=sorted(set(descartados)),
        avisos=avisos,
    )


SYSTEM_PROMPT = """\
Lees un tique de compra o una factura de un consumidor español. Localiza solo estos datos, si constan de forma \
expresa: `descripcion` (qué se compró: el artículo principal, tal como figura), `establecimiento` (el vendedor), \
`fecha_compra` (la fecha del tique o de la factura, en formato AAAA-MM-DD), `fecha_entrega` (solo si figura un \
albarán o fecha de entrega distinta) e `importe` (el total pagado, con punto decimal). Para cada dato devuelve \
`campo`, `valor` y `evidencia`: el fragmento LITERAL del documento, copiado tal cual, donde aparece. No deduzcas \
nada que no esté escrito. No copies números de tarjeta, NIF ni otros identificadores.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "campos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "campo": {"type": "string", "enum": list(CAMPOS)},
                    "valor": {"type": "string"},
                    "evidencia": {"type": "string"},
                },
                "required": ["campo", "valor", "evidencia"],
            },
        },
    },
    "required": ["campos"],
}

SYSTEM_PROMPT_IMAGEN = (
    SYSTEM_PROMPT
    + "El documento llega como imagen: en `transcripcion` copia todo el texto legible tal cual; la `evidencia` debe "
    "ser un fragmento literal de tu transcripción. Si un dato no se lee con claridad, no lo incluyas.\n"
)
SCHEMA_IMAGEN: dict[str, Any] = {
    **SCHEMA,
    "properties": {"transcripcion": {"type": "string"}, **SCHEMA["properties"]},
    "required": ["transcripcion", "campos"],
}


def _leer_imagen(provider: Any, adjunto: Attachment, filename: str) -> PropuestaCompra:
    lecturas: list[tuple[dict[str, CampoPropuesto], list[str]]] = []
    for _ in range(2):
        raw = provider.complete_json(
            system=SYSTEM_PROMPT_IMAGEN,
            user=f'El documento adjunto se llama "{filename}". Transcríbelo y localiza los datos de la compra.',
            schema=SCHEMA_IMAGEN,
            schema_name="datos_tique_escaneado",
            attachments=[adjunto],
        )
        transcripcion = raw.get("transcripcion") if isinstance(raw, dict) else ""
        campos = raw.get("campos") if isinstance(raw, dict) else []
        lecturas.append(_validar(campos, _normalize(transcripcion if isinstance(transcripcion, str) else ""), "imagen"))
    (primera, desc_1), (segunda, desc_2) = lecturas
    coinciden = {c: v for c, v in primera.items() if c in segunda and segunda[c].valor == v.valor}
    discrepan = sorted((set(primera) | set(segunda)) - set(coinciden))
    avisos = [
        "Documento leído a partir de la imagen: revisa cada dato con el papel antes de registrar la compra.",
    ]
    if discrepan:
        avisos.append("Dos lecturas independientes no coinciden en: " + ", ".join(discrepan) + ". Introdúcelos a mano.")
    orden = {campo: i for i, campo in enumerate(CAMPOS)}
    return PropuestaCompra(
        campos=sorted(coinciden.values(), key=lambda c: orden[c.campo]),
        descartados=sorted(set(desc_1) | set(desc_2) | set(discrepan)),
        avisos=avisos,
    )


def leer_ticket(provider: Any, *, filename: str, data: bytes) -> PropuestaCompra:
    """Propose a purchase from an uploaded receipt. Raises ``DocumentoNoLegibleError`` or the provider's error."""
    imagen = tipo_de_imagen(data)
    if imagen is not None:
        return _leer_imagen(provider, Attachment(imagen, data, filename), filename)
    try:
        texto, _ = extract_text(data, filename)
    except DocumentoEscaneadoError as exc:
        if exc.paginas > 5:
            raise DocumentoNoLegibleError("Un tique escaneado de más de 5 páginas no se lee automáticamente.") from exc
        return _leer_imagen(provider, Attachment("application/pdf", data, filename), filename)
    raw = provider.complete_json(
        system=SYSTEM_PROMPT,
        user=f'<documento nombre="{filename}">\n{texto[:MAX_TEXTO]}\n</documento>',
        schema=SCHEMA,
        schema_name="datos_tique",
    )
    campos = raw.get("campos") if isinstance(raw, dict) else []
    return verificar_propuesta(texto, campos if isinstance(campos, list) else [])
