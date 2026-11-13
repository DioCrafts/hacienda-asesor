"""What does the citing document do with the cited one?

Two layers:

* A **heuristic** reading of the passage around the citation — cheap,
  deterministic, and deliberately conservative: it only asserts ``supera`` or
  ``contradice`` on explicit wording.
* An optional **LLM** reading for the citations where it matters (a later,
  higher-ranking document citing doctrine): it must quote the words that
  justify its verdict, and a verdict whose quote is not in the passage is
  discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import unicodedata

from hacienda_gpt.doctrine.grafo import RELACIONES, Relacion

_SUPERA = (
    r"cambi[oa] de criterio",
    r"modific\w+ (?:el|su|dicho|este) criterio",
    r"rectific\w+ (?:el|su|dicho|la) (?:criterio|doctrina)",
    r"(?:criterio|doctrina) (?:que )?(?:ha |han |debe \w+ |queda \w* ?)?superad[oa]",
    r"ha (?:de entenderse|quedado) superad[oa]",
    r"qued\w+ sin efecto",
    r"dej\w+ sin efecto",
    r"anul\w+ (?:la|dicha|esta) (?:resoluci[oó]n|liquidaci[oó]n|doctrina)",
    r"no (?:puede|cabe) mantener(?:se)? (?:el|dicho|este) criterio",
    r"abandon\w+ (?:el|su|dicho|este) criterio",
    r"en contra de lo sostenido por",
    r"corrige (?:el|la) (?:criterio|doctrina)",
)
_CONTRADICE = (
    r"no (?:se )?compart\w+ (?:el|dicho|este|tal) criterio",
    r"en contra del criterio",
    r"criterio contrario",
    r"discrep\w+",
    r"frente a(?:l)? (?:criterio|lo sostenido|lo mantenido)",
    r"a diferencia de lo (?:sostenido|mantenido|afirmado)",
)
_MATIZA = (
    r"matiz\w+",
    r"con la salvedad",
    r"si bien (?:debe|ha de|cabe) precis",
    r"precis\w+ (?:el|dicho|este) criterio",
)
_CONFIRMA = (
    r"en el mismo sentido",
    r"en (?:este|igual|id[ée]ntico) sentido",
    r"reiter\w+",
    r"conforme a(?:l)? (?:criterio|la doctrina)",
    r"siguiendo (?:el|la) (?:criterio|doctrina)",
    r"de acuerdo con (?:el criterio|la doctrina|la jurisprudencia)",
    r"como (?:ha )?(?:señalado|declarado|establecido|fijado)",
    r"en aplicaci[oó]n de (?:la doctrina|el criterio|la jurisprudencia)",
    r"asum\w+ (?:el|dicho|este) criterio",
)


@dataclass(frozen=True)
class Lectura:
    relacion: Relacion
    confianza: float
    evidencia: str
    metodo: str


def _first(patterns: tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def leer_heuristica(contexto: str) -> Lectura:
    """Relation suggested by explicit wording in the passage (default: ``cita``)."""
    for relacion, patterns, confianza in (
        ("supera", _SUPERA, 0.6),
        ("contradice", _CONTRADICE, 0.5),
        ("matiza", _MATIZA, 0.5),
        ("confirma", _CONFIRMA, 0.6),
    ):
        hit = _first(patterns, contexto)
        if hit:
            return Lectura(relacion, confianza, hit, "heuristica")  # type: ignore[arg-type]
    return Lectura("cita", 0.9, "", "heuristica")


LLM_SYSTEM_PROMPT = """\
Analizas doctrina tributaria española. Recibes un pasaje de un documento (una sentencia, una resolución del TEAC \
o una consulta de la DGT) en el que se menciona otra resolución anterior, identificada en la petición. Decide qué \
hace el documento que cita con la resolución citada:

- "confirma": la sigue, la reitera o aplica su criterio.
- "matiza": mantiene el criterio pero lo precisa, lo limita o introduce una excepción.
- "supera": la revoca, la anula, declara que su criterio no es correcto o fija un criterio que la deja sin efecto.
- "contradice": llega a la conclusión contraria sobre la misma cuestión, sin anularla ni tener autoridad para ello.
- "cita": la menciona sin pronunciarse sobre su criterio (antecedentes, alegaciones de las partes, cita de pasada).

Sé estricto: "supera" y "contradice" solo cuando el pasaje lo diga con claridad sobre ESA resolución; si el \
pasaje recoge lo que alega una parte, o no deja claro el sentido, responde "cita". En `cita_literal` copia tal \
cual las palabras del pasaje que justifican tu respuesta (cadena vacía si es "cita").
"""

LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relacion": {"type": "string", "enum": list(RELACIONES)},
        "cita_literal": {"type": "string"},
        "motivo": {"type": "string"},
    },
    "required": ["relacion", "cita_literal", "motivo"],
}


def _normalize(text: str) -> str:
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return re.sub(r"\s+", "", plain).lower()


def leer_llm(provider: Any, *, origen: str, destino: str, contexto: str) -> Lectura | None:
    """LLM reading of one citation; None when it cannot be trusted.

    Raises the provider's error on failure so the caller can stop a paid batch.
    """
    data = provider.complete_json(
        system=LLM_SYSTEM_PROMPT,
        user=(f"Documento que cita: {origen}\nResolución citada: {destino}\n\n<pasaje>\n{contexto}\n</pasaje>"),
        schema=LLM_SCHEMA,
        schema_name="relacion_doctrinal",
    )
    relacion = data.get("relacion")
    if relacion not in RELACIONES:
        return None
    quote = data.get("cita_literal") if isinstance(data.get("cita_literal"), str) else ""
    if relacion == "cita":
        return Lectura("cita", 0.9, "", "llm")
    # A typed relation must be anchored in words that are really in the passage.
    if len(_normalize(quote)) < 8 or _normalize(quote) not in _normalize(contexto):
        return None
    return Lectura(relacion, 0.85, " ".join(quote.split())[:300], "llm")
