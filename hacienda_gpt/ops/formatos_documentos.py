"""Reader drift for the user's own documents — without keeping anything of theirs.

Payslips and certificates change layout and wording like any other format, but
they are personal data: ``docs/privacidad.md`` promises they are processed in
memory and never stored. So the watch works on two things only:

* COUNTERS per kind of document: how many facts were proposed and the NAMES
  (never the values) of those discarded by the literal-quote check — the same
  information the audit trail already keeps;
* optionally (``FORMAT_DOCUMENT_SKELETONS``, off by default) an anonymised
  SKELETON of a document whose reading failed: its layout with every token
  blanked except words from a fixed list of fiscal labels. The rule is an
  allow-list, not a block-list: a name, an address, a tax id or a figure can
  only survive if it is itself one of those generic words.

This module has no dependency on the tax package (which imports it).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import unicodedata

from hacienda_gpt import settings
from hacienda_gpt.ops import formatos

logger = logging.getLogger(__name__)

MAX_LINEAS = 80
MAX_TOKENS_LINEA = 16
MAX_ESQUELETOS = 3
HUECO_PALABRA = "_"
HUECO_CIFRA = "#"

# Kind of document → words (unaccented, lower case) that identify it in the reader's own description.
CLASES: dict[str, tuple[str, ...]] = {
    "nomina": ("nomina", "recibo de salario"),
    "certificado_retenciones": ("retenciones", "certificado de empresa", "modelo 190"),
    "datos_fiscales": ("datos fiscales",),
    "plan_pensiones": ("plan de pensiones", "planes de pensiones", "prevision social"),
    "donativos": ("donativo", "donacion"),
    "broker": ("broker", "valores", "extracto", "inversion"),
    "libro_registro": ("libro registro", "libro de ingresos", "libro de gastos", "facturas"),
    "modelo_130": ("modelo 130", "pago fraccionado"),
    "declaracion_renta": ("declaracion de la renta", "modelo 100", "renta web"),
}
OTROS = "otros"

# Generic words of Spanish fiscal paperwork (labels and connectors). Deliberately free of anything
# that names a person, a company or a place.
_ETIQUETAS = """
a al ano anual anuales aportacion aportaciones autonomo autonomos base bases bruto bruta brutos cargo certificado
certificacion clave codigo comunes concepto conceptos con contingencias contribucion contribuciones cotizacion
cotizaciones cuenta cuota cuotas datos de del deducciones deducible deducibles derivados descuento descuentos
devengado devengados devengo devengos dinerarias dinerario dividendos documento donativo donativos ejercicio el
empresa en entidad especie euros exentas exento expedicion extra fecha fiscal fiscales gasto gastos imponible importe
importes impuesto incapacidad incluidos ingreso ingresos integra integras integro integros intereses irpf iva la las
liquidacion liquido los mensual mes modelo neto neta netos no nomina numero o otros paga pagador pagos para participe
pension pensiones percepcion percepciones perceptor percibir periodo personal plan por porcentaje practicadas
prestaciones profesionales promotor reducciones registro rendimiento rendimientos renta resumen retencion retenciones
retribucion retribuciones salario salarial salariales seguridad sin sobre social subtotal suma tipo titular total
totales trabajador trabajo trimestre tributacion valor y
"""
ETIQUETAS: frozenset[str] = frozenset(_ETIQUETAS.split())

_TOKEN = re.compile(r"[^\W\d_]+|\d[\d.,/:%-]*|[^\w\s]", re.UNICODE)


def plano(text: str) -> str:
    """Lower case, without accents."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def clase_de(tipo_documento: str) -> str:
    """Kind of document from the reader's free-text description; ``otros`` when unknown."""
    texto = plano(tipo_documento)
    return next((clase for clase, marcas in CLASES.items() if any(marca in texto for marca in marcas)), OTROS)


def palabras_permitidas(text: str) -> bool:
    """True when every word of ``text`` is a generic fiscal label (nothing else may enter a prompt or a file)."""
    palabras = re.findall(r"[^\W\d_]+", plano(text), re.UNICODE)
    return bool(palabras) and all(palabra in ETIQUETAS for palabra in palabras)


def esqueleto(text: str) -> str:
    """Layout of a document with every value blanked: only allow-listed label words survive.

    Each line keeps its order of tokens: a known label word as is (lower case,
    unaccented), any other word as ``_``, anything with a digit as ``#``, and
    ``:`` as itself. Repeated gaps are collapsed, so not even the number of
    words of a name is kept.
    """
    lineas: list[str] = []
    for linea in text.splitlines():
        partes: list[str] = []
        for token in _TOKEN.findall(linea):
            if token[0].isdigit():
                parte = HUECO_CIFRA
            elif token[0].isalpha():
                palabra = plano(token)
                parte = palabra if palabra in ETIQUETAS else HUECO_PALABRA
            elif token == ":":
                parte = ":"
            else:
                continue
            if parte in (HUECO_PALABRA, HUECO_CIFRA) and partes and partes[-1] == parte:
                continue
            partes.append(parte)
        if any(parte not in (HUECO_PALABRA, HUECO_CIFRA, ":") for parte in partes):
            lineas.append(" ".join(partes[:MAX_TOKENS_LINEA]))
        if len(lineas) >= MAX_LINEAS:
            break
    return "\n".join(lineas)


def _ruta_esqueletos(clase: str, directorio: str | Path | None = None) -> Path:
    base = Path(directorio) if directorio is not None else Path(settings.FORMAT_DIR)
    return base / "esqueletos" / f"{clase}.json"


def leer_esqueletos(clase: str, directorio: str | Path | None = None) -> list[str]:
    try:
        data = json.loads(_ruta_esqueletos(clase, directorio).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []


def observar_lectura(
    *,
    tipo_documento: str,
    propuestos: int,
    descartados: list[str],
    lectura: str,
    texto: str | None = None,
) -> None:
    """Record the outcome of reading one user document. Called from the reader: NEVER raises.

    Only counters and field names reach the state file. ``texto`` is used, when
    skeletons are enabled and the reading failed, to derive the anonymised
    skeleton; the text itself is never written anywhere.
    """
    if not settings.FORMAT_WATCH:
        return
    try:
        clase = clase_de(tipo_documento)
        observacion = formatos.observar(
            f"doc_{clase}",
            None,
            extraccion={"propuestos": propuestos, "descartados": sorted(set(descartados)), "lectura": lectura},
        )
        if (
            observacion is not None
            and not observacion.ok
            and texto
            and settings.FORMAT_DOCUMENT_SKELETONS
            and (forma := esqueleto(texto))
        ):
            ruta = _ruta_esqueletos(clase)
            guardados = [*leer_esqueletos(clase), forma][-MAX_ESQUELETOS:]
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(json.dumps(guardados, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except Exception:
        logger.warning("Could not record the document-reader observation", exc_info=True)
