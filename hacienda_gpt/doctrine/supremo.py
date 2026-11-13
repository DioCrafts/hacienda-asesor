"""Selective analysis of Tribunal Supremo rulings (Sala Tercera, Sección Segunda).

The advisor does not ingest Supreme Court case law in bulk. It only wants the
rulings of the tax section (Sala Tercera, Sección Segunda) and, among those,
gives priority to cassation appeals decided under the regime introduced by
LO 7/2015 (in force since 2016-07-22), where the Court answers a question with
"interés casacional objetivo para la formación de la jurisprudencia" and
normally states its doctrine in a dedicated legal ground.

This module is pure: no network and no LLM. It takes the HTML of ONE ruling,
reuses :func:`hacienda_gpt.crawler.cendoj.parse_cendoj_document` for the basic
metadata and adds what that parser does not know about: section, kind of
appeal, appeal/judgment numbers, the admission question and the doctrine
passage, plus a deterministic priority score with human-readable reasons.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Literal
import unicodedata

import parsel
from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.crawler.cendoj import parse_cendoj_document

# First day of the cassation regime of LO 7/2015 (disposición final décima).
FECHA_NUEVA_CASACION = date(2016, 7, 22)
SECCION_TRIBUTARIA = 2
MAX_PASSAGE_CHARS = 1500
DOCUMENT_TYPE = "sentencia_ts"

PRIORIDAD_CASACION_NUEVA_CON_DOCTRINA = 100
PRIORIDAD_CASACION_NUEVA_SIN_DOCTRINA = 70
PRIORIDAD_CASACION_ANTERIOR = 40
PRIORIDAD_OTRO_RECURSO = 30
PRIORIDAD_NO_OBJETIVO = 0

_SALA_TERCERA = "Sala Tercera (Contencioso-Administrativo)"
# Second digit of the court code embedded in the "Id Cendoj" identifier, for
# Tribunal Supremo documents (first digit "1").
_SALAS_POR_CODIGO = {
    "1": "Sala Primera (Civil)",
    "2": "Sala Segunda (Penal)",
    "3": _SALA_TERCERA,
    "4": "Sala Cuarta (Social)",
    "5": "Sala Quinta (Militar)",
}
_OTRAS_SALAS_POR_NOMBRE = {
    "civil": "Sala Primera (Civil)",
    "primera": "Sala Primera (Civil)",
    "penal": "Sala Segunda (Penal)",
    "segunda": "Sala Segunda (Penal)",
    "social": "Sala Cuarta (Social)",
    "cuarta": "Sala Cuarta (Social)",
    "militar": "Sala Quinta (Militar)",
    "quinta": "Sala Quinta (Militar)",
}

# How far into the document the header block (court, section, numbers) is
# expected to live. Mentions further down are usually citations of other
# rulings or of the admission section (Sección Primera), not of this ruling.
_HEAD_CHARS = 2000

_BLOCK_BREAK_RE = re.compile(
    r"<br\s*/?>|</?(?:p|div|h[1-6]|li|tr|td|th|table|section|article|header|footer|title|ul|ol|blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)

_ID_CENDOJ_RE = re.compile(r"Id\.?\s*Cendoj\s*:?\s*(\d{20})\b", re.IGNORECASE)
_SALA_HEAD_RE = re.compile(
    r"Sala\s+(?:de\s+lo\s+)?(Civil|Penal|Social|Militar|Contencioso|Primera|Segunda|Tercera|Cuarta|Quinta)\b",
    re.IGNORECASE,
)
_SECCION_LABEL_RE = re.compile(r"Secci[oó]n\s*:\s*(\d{1,2})\b", re.IGNORECASE)
_SECCION_ORDINAL_RE = re.compile(
    r"Secci[oó]n\s+(?:(Primera|Segunda|Tercera|Cuarta|Quinta|Sexta|S[eé]ptima|Octava)\b|(\d{1,2})\s*\.?\s*[ªa]?(?!\w))",
    re.IGNORECASE,
)
_SECCIONES = {
    "primera": 1,
    "segunda": 2,
    "tercera": 3,
    "cuarta": 4,
    "quinta": 5,
    "sexta": 6,
    "septima": 7,
    "octava": 8,
}

_FECHA_LABEL_RE = re.compile(
    r"Fecha(?:\s+de\s+(?:la\s+)?sentencia)?\s*:\s*(\d{2})[/-](\d{2})[/-](\d{4})\b", re.IGNORECASE
)

_NUM = r"(\d{1,3}(?:\.\d{3})+|\d{1,6})\s*/\s*((?:19|20)\d{2})\b"
_NUM_LABEL = r"(?:n[uú]m(?:ero)?s?\.?|n\.?\s*[ºo°]\.?|nº)\s*:?\s*"
# Ordered from the most to the least reliable form. "rec. N/AAAA" comes last
# because it is also the usual way of citing OTHER rulings in the reasoning.
_NUMERO_RECURSO_RES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"(?:N[ºo°]\.?|N[uú]mero)\s+de\s+Recurso\s*:\s*{_NUM}",
        rf"N[uú]mero\s+del\s+procedimiento\s*:\s*{_NUM}",
        rf"R\.\s*CASACI[OÓ]N\s+{_NUM_LABEL}{_NUM}",
        rf"\bRCA\s*[/\-\s]\s*{_NUM}",
        rf"recurso\s+de\s+casaci[oó]n\s+(?:contencioso-administrativo\s+)?(?:{_NUM_LABEL})?{_NUM}",
        rf"\brec(?:urso)?\.?\s+(?:de\s+casaci[oó]n\s+)?(?:{_NUM_LABEL})?{_NUM}",
    )
)
_NUMERO_SENTENCIA_RES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"(?:N[ºo°]\.?|N[uú]mero)\s+de\s+Resoluci[oó]n\s*:\s*{_NUM}",
        rf"\bSentencia\s+{_NUM_LABEL}{_NUM}",
        rf"\bSTS\s+{_NUM_LABEL}{_NUM}",
    )
)

_PROCEDIMIENTO_RE = re.compile(
    r"(?:Tipo\s+de\s+procedimiento|Procedimiento)\s*:\s*([^\n]{1,140})",
    re.IGNORECASE,
)
_NEXT_LABEL_RE = re.compile(r"\s(?:Ponente|Tipo\s+de\s+Resoluci[oó]n|N[uú]mero\s+del\s+procedimiento|Fecha)\b", re.I)
_CASACION_RE = re.compile(r"(?:recurso\s+de|R\.)\s*casaci[oó]n\b|\bRCA\s*/\s*\d", re.IGNORECASE)
_UNIFICACION_RE = re.compile(r"unificaci[oó]n\s+de\s+doctrina|inter[eé]s\s+de\s+(?:la\s+)?ley", re.IGNORECASE)
_OTRO_RECURSO_RE = re.compile(
    r"recurso\s+(?:de\s+revisi[oó]n|contencioso-administrativo|ordinario|de\s+queja|de\s+apelaci[oó]n)"
    r"|REC\.\s*ORDINARIO|cuesti[oó]n\s+de\s+ilegalidad|error\s+judicial",
    re.IGNORECASE,
)
_INTERES_CASACIONAL_RE = re.compile(r"inter[eé]s\s+casacional\s+objetivo", re.IGNORECASE)

_ORDINALES = (
    "PRIMERO",
    "SEGUNDO",
    "TERCERO",
    "CUARTO",
    "QUINTO",
    "SEXTO",
    "SEPTIMO",
    "OCTAVO",
    "NOVENO",
    "DECIMO",
    "UNDECIMO",
    "DUODECIMO",
    "DECIMOTERCERO",
    "DECIMOCUARTO",
    "DECIMOQUINTO",
    "DECIMOSEXTO",
    "DECIMOSEPTIMO",
    "DECIMOCTAVO",
    "DECIMONOVENO",
    "VIGESIMO",
)
_ORDINAL_ALTERNATION = (
    r"DECIMO(?:TERCERO|CUARTO|QUINTO|SEXTO|S[EÉ]PTIMO|CTAVO|NOVENO)|UND[EÉ]CIMO|DUOD[EÉ]CIMO|D[EÉ]CIMO|VIG[EÉ]SIMO"
    r"|PRIMERO|SEGUNDO|TERCERO|CUARTO|QUINTO|SEXTO|S[EÉ]PTIMO|OCTAVO|NOVENO"
)
# Numbered grounds are written in capitals ("QUINTO.- ..."); matching is
# case-sensitive on purpose so "primero" inside a sentence is not a heading.
_HEADING_RE = re.compile(
    rf"(?<![\wÁÉÍÓÚÑ])(?:(?P<ordinal>{_ORDINAL_ALTERNATION})\s*[.\-–—:]"
    r"|(?P<fallo>F\s?A\s?L\s?L\s?O|PARTE\s+DISPOSITIVA)(?![\wÁÉÍÓÚÑ])"
    r"|(?P<seccion>FUNDAMENTOS\s+(?:DE\s+DERECHO|JUR[IÍ]DICOS)|ANTECEDENTES\s+DE\s+HECHO|VOTO\s+PARTICULAR))"
)
_FUNDAMENTOS_RE = re.compile(r"FUNDAMENTOS\s+(?:DE\s+DERECHO|JUR[IÍ]DICOS)")

# Titles of the legal ground where the Court states its interpretation.
_DOCTRINE_HEADING_RE = re.compile(
    r"criterios?\s+interpretativos?"
    r"|contenido\s+interpretativo\s+de\s+(?:esta|la)\s+sentencia"
    r"|respuesta\s+a\s+la(?:s)?\s+cuesti[oó]n(?:es)?\s+(?:interpretativas?|con\s+inter[eé]s\s+casacional|casacional)"
    r"|(?:fijaci[oó]n\s+de\s+(?:la\s+)?doctrina|doctrina\s+(?:jurisprudencial\s+)?que\s+se\s+(?:fija|establece))",
    re.IGNORECASE,
)
# First-person / operative wording only: a ruling that merely discusses the
# "doctrina" of another court never uses these forms about it.
_DOCTRINE_PHRASE_RE = re.compile(
    r"\b(?:fijamos|se\s+fijan?|fijar|fijando|establecemos|declaramos)\s+(?:,\s*)?"
    r"(?:como\s+|la\s+siguiente\s+|el\s+siguiente\s+|los\s+siguientes\s+)"
    r"(?:doctrina|criterios?\s+interpretativos?|criterio)"
    r"|\bla\s+respuesta\s+a\s+la\s+(?:\w+\s+){0,2}?cuesti[oó]n[^.\n]{0,200}?\b(?:debe|ha\s+de|tiene\s+que)\s+ser\b"
    r"|\bprocede\s+declarar\s+lo\s+siguiente"
    r"|\bdoctrina\s+(?:jurisprudencial\s+)?que\s+(?:fijamos|establecemos|declaramos)",
    re.IGNORECASE,
)
_FALLO_CRITERIOS_RE = re.compile(
    r"\b(?:fijar|fijamos|se\s+fijan)\s+(?:como\s+)?(?:los\s+)?criterios\s+interpretativos" r"(?P<resto>[^.\n]{0,160})",
    re.IGNORECASE,
)
_FUNDAMENTO_REF_RE = re.compile(
    r"fundamentos?\s+(?:jur[ií]dicos?|de\s+derecho)\s+(?P<ref>[a-záéíóú]+|\d{1,2})",
    re.IGNORECASE,
)

_CUESTION_RE = re.compile(
    r"cuesti[oó]n(?:es)?\s+(?:que\s+)?(?:presentan?|revisten?|tienen?|suscitan?)\s+inter[eé]s\s+casacional"
    r"(?:\s+objetivo)?(?:\s+para\s+la\s+formaci[oó]n\s+de\s+(?:la\s+)?jurisprudencia)?"
    r"\s*,?\s*(?:consisten?\s+en|es|son|se\s+concreta\s+en)?(?:\s+l[ao]s?\s+siguientes?)?\s*[:.]?\s*",
    re.IGNORECASE,
)
_CUESTION_STOP_RE = re.compile(
    r"(?:\d\s*[ºo°.]?\s*\)\s*)?Identificar\s+como\s+normas?|(?:\d\s*[ºo°.]?\s*\)\s*)?Publicar\s+este\s+auto",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^(?:\d+\s*[.ºo°ª)]|[a-z]\)|[-–•«\"“(])")

# Sentence end: a full stop that does not close a common legal abbreviation
# or a number ("art. 13", "núm. 4", "1.456"), followed by a capital or a quote.
_SENTENCE_END_RE = re.compile(
    r"(?<!\bart)(?<!\barts)(?<!\bn[uú]m)(?<!\bp[aá]g)(?<!\bcfr)(?<!\brec)(?<!\bss)(?<!\bapdo)(?<!\b[A-Za-z])"
    r"\.[»\"”)]?(?=\s+[A-ZÁÉÍÓÚÑ«\"“(\d]|\s*$)"
)


class SentenciaTS(BaseModel):
    """Structured view of one Tribunal Supremo ruling, as far as it could be read."""

    model_config = ConfigDict(extra="forbid")

    roj: str | None = None
    ecli: str | None = None
    fecha: date | None = None
    sala: str | None = None
    seccion: int | None = None
    tipo_recurso: Literal["casacion", "otro"] | None = None
    numero_recurso: str | None = None
    numero_sentencia: str | None = None
    es_tributaria: bool = False
    tributos: list[str] = Field(default_factory=list)
    fija_doctrina: bool = False
    doctrina: str | None = None
    cuestion_casacional: str | None = None
    prioridad: int = Field(default=0, ge=0, le=100)
    motivos: list[str] = Field(default_factory=list)


def analizar_sentencia(html: str, *, source_url: str | None = None) -> SentenciaTS:
    """Analyse the HTML of one ruling and return its structured description."""
    sentencia, _ = analizar_documento(html, source_url=source_url)
    return sentencia


def analizar_documento(html: str, *, source_url: str | None = None) -> tuple[SentenciaTS, dict[str, Any]]:
    """Like :func:`analizar_sentencia`, also returning the base CENDOJ metadata.

    The second element is the untouched dict from ``parse_cendoj_document``,
    which is what :func:`sidecar_metadata` expects as ``base``.
    """
    base = parse_cendoj_document(html, source_url=source_url)
    text = _extract_text(html)
    head = _header_block(text)
    id_cendoj = _ID_CENDOJ_RE.search(text)

    fecha = _extract_fecha(head) or _parse_iso_date(base.get("fecha_resolucion"))
    sentencia = SentenciaTS(
        roj=base.get("roj"),
        ecli=base.get("ecli"),
        fecha=fecha,
        sala=_detect_sala(head, base.get("sala"), id_cendoj.group(1) if id_cendoj else None),
        seccion=_detect_seccion(head, id_cendoj.group(1) if id_cendoj else None),
        tipo_recurso=_detect_tipo_recurso(text),
        numero_recurso=_first_number(_NUMERO_RECURSO_RES, text),
        numero_sentencia=_first_number(_NUMERO_SENTENCIA_RES, text),
        es_tributaria=bool(base.get("is_tributario")),
        tributos=list(base.get("tributos_detectados") or []),
        cuestion_casacional=_extract_cuestion(text),
    )
    sentencia.fija_doctrina, sentencia.doctrina = _extract_doctrina(text)
    sentencia.prioridad, sentencia.motivos = _puntuar(
        sentencia, menciona_interes=bool(_INTERES_CASACIONAL_RE.search(text))
    )
    return sentencia, base


def es_objetivo_prioritario(s: SentenciaTS) -> bool:
    """True only for tax rulings of Sala Tercera, Sección Segunda.

    An undetermined section does not qualify: the tool prefers to miss a
    ruling over ingesting case law of other sections by accident.
    """
    return _es_sala_tercera(s.sala) and s.seccion == SECCION_TRIBUTARIA and s.es_tributaria


def es_casacion_nueva(s: SentenciaTS) -> bool:
    """True for cassation appeals decided on or after 2016-07-22 (LO 7/2015 regime)."""
    return s.tipo_recurso == "casacion" and s.fecha is not None and s.fecha >= FECHA_NUEVA_CASACION


def calcular_prioridad(s: SentenciaTS) -> int:
    """Deterministic 0-100 priority of a ruling for the doctrine corpus.

    ===  =====================================================================
    100  Sala 3ª, Sección 2ª, tax; cassation decided from 2016-07-22 onward in
         which a doctrine passage was detected (``fija_doctrina``).
     70  Same, but no doctrine passage was detected.
     40  Sala 3ª, Sección 2ª, tax; cassation dated before 2016-07-22 or undated.
     30  Sala 3ª, Sección 2ª, tax; any other (or unidentified) kind of appeal.
      0  Everything else (other Sala, other or unknown section, not tax).
    ===  =====================================================================

    Only the fields of ``s`` are read, so the score can be recomputed from a
    stored ``SentenciaTS``.
    """
    if not es_objetivo_prioritario(s):
        return PRIORIDAD_NO_OBJETIVO
    if es_casacion_nueva(s):
        return PRIORIDAD_CASACION_NUEVA_CON_DOCTRINA if s.fija_doctrina else PRIORIDAD_CASACION_NUEVA_SIN_DOCTRINA
    if s.tipo_recurso == "casacion":
        return PRIORIDAD_CASACION_ANTERIOR
    return PRIORIDAD_OTRO_RECURSO


def sidecar_metadata(s: SentenciaTS, base: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON sidecar that the indexer merges into every chunk.

    Starts from the dict produced by ``parse_cendoj_document`` (``base`` is
    not mutated; ``source_authority`` is kept as the parser set it) and adds
    the doctrine fields. ``fecha_resolucion`` is aligned with ``s.fecha`` when
    the labelled date of the header differs from the parser's first-date guess.
    """
    metadata = dict(base)
    if s.fecha is not None:
        metadata["fecha_resolucion"] = s.fecha.isoformat()
    metadata.update(
        {
            "seccion": s.seccion,
            "tipo_recurso": s.tipo_recurso,
            "numero_recurso": s.numero_recurso,
            "numero_sentencia": s.numero_sentencia,
            "fija_doctrina": s.fija_doctrina,
            "doctrina_fijada": s.doctrina,
            "cuestion_casacional": s.cuestion_casacional,
            "prioridad_doctrinal": s.prioridad,
            "document_type": DOCUMENT_TYPE,
        }
    )
    return metadata


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _puntuar(s: SentenciaTS, *, menciona_interes: bool) -> tuple[int, list[str]]:
    """Return the priority of ``s`` and the Spanish reasons that explain it."""
    prioridad = calcular_prioridad(s)
    motivos: list[str] = []

    if not es_objetivo_prioritario(s):
        if not _es_sala_tercera(s.sala):
            motivos.append(
                f"No es de la Sala Tercera (Contencioso-Administrativo): sala detectada «{s.sala}»."
                if s.sala
                else "No se ha podido determinar la sala; no consta que sea de la Sala Tercera."
            )
        if s.seccion is None:
            motivos.append("No se ha podido determinar la sección; no se considera de la Sección Segunda.")
        elif s.seccion != SECCION_TRIBUTARIA:
            motivos.append(f"Es de la Sección {s.seccion}, no de la Sección Segunda (tributaria).")
        if not s.es_tributaria:
            motivos.append("No se aprecia materia tributaria en el texto.")
        motivos.append("Se descarta: solo se incorporan sentencias tributarias de la Sala Tercera, Sección Segunda.")
        return prioridad, motivos

    tributos = f" ({', '.join(s.tributos)})" if s.tributos else ""
    motivos.append(f"Sentencia de la Sala Tercera, Sección Segunda, en materia tributaria{tributos}.")
    if es_casacion_nueva(s):
        motivos.append(
            f"Recurso de casación resuelto el {_fecha_es(s.fecha)}, "
            "bajo el régimen de la LO 7/2015 (desde el 22/07/2016)."
        )
        if not menciona_interes:
            motivos.append(
                "Aviso: el texto no menciona el interés casacional objetivo; podría ser un recurso "
                "preparado bajo el régimen anterior y resuelto después del 22/07/2016."
            )
        if s.cuestion_casacional:
            motivos.append("Se ha identificado la cuestión con interés casacional objetivo.")
        if s.fija_doctrina:
            motivos.append(
                "Fija doctrina: se ha localizado el pasaje con los criterios interpretativos."
                if s.doctrina
                else "Fija doctrina según el fallo, aunque no se ha podido extraer el pasaje."
            )
            motivos.append(f"Prioridad máxima ({prioridad}).")
        else:
            motivos.append(f"No se ha detectado un pasaje que fije doctrina: prioridad alta ({prioridad}).")
    elif s.tipo_recurso == "casacion":
        if s.fecha is None:
            motivos.append("Recurso de casación sin fecha reconocible: no consta que sea del régimen de la LO 7/2015.")
        else:
            motivos.append(
                f"Recurso de casación resuelto el {_fecha_es(s.fecha)}, anterior al régimen de la LO 7/2015 "
                "(22/07/2016)."
            )
        motivos.append(f"Prioridad media ({prioridad}).")
    else:
        motivos.append(
            "No es un recurso de casación."
            if s.tipo_recurso == "otro"
            else "No se ha podido determinar el tipo de recurso."
        )
        motivos.append(f"Prioridad baja ({prioridad}).")
    return prioridad, motivos


def _es_sala_tercera(sala: str | None) -> bool:
    lowered = (sala or "").lower()
    return "tercera" in lowered or "contencioso" in lowered


def _fecha_es(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "fecha desconocida"


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _extract_text(html: str) -> str:
    """Return the visible body text with one line per block element.

    Whitespace inside a block (including source line wraps) is collapsed so
    phrases can be matched across wrapped lines, while block boundaries are
    kept as newlines so header fields and headings stay delimited.
    """
    marked = _BLOCK_BREAK_RE.sub(lambda match: f"\n{match.group(0)}", re.sub(r"\s+", " ", html))
    selector = parsel.Selector(text=marked)
    nodes = selector.xpath("//body//text()[not(ancestor::script) and not(ancestor::style)]").getall()
    lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(nodes).split("\n"))
    return "\n".join(line for line in lines if line)


def _header_block(text: str) -> str:
    """Return the identification block that precedes the body of the ruling."""
    antecedentes = text.find("ANTECEDENTES DE HECHO")
    return text[: min(_HEAD_CHARS, antecedentes) if antecedentes != -1 else _HEAD_CHARS]


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _extract_fecha(head: str) -> date | None:
    match = _FECHA_LABEL_RE.search(head)
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _strip_accents(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", value) if unicodedata.category(ch) != "Mn")


# ---------------------------------------------------------------------------
# Court, section, appeal
# ---------------------------------------------------------------------------


def _detect_sala(head: str, parser_sala: str | None, id_cendoj: str | None) -> str | None:
    """Resolve the chamber, correcting the base parser where the header disagrees.

    The base parser only recognises Sala Tercera and takes its first mention
    anywhere in the text, so a ruling of another chamber that cites the Sala
    Tercera would look like one. The "Id Cendoj" court code and the first
    chamber named in the header take precedence over that.
    """
    if id_cendoj and id_cendoj[5] == "1" and id_cendoj[6] in _SALAS_POR_CODIGO:
        return _SALAS_POR_CODIGO[id_cendoj[6]]
    match = _SALA_HEAD_RE.search(head)
    if match:
        nombre = match.group(1).lower()
        if nombre in _OTRAS_SALAS_POR_NOMBRE:
            return _OTRAS_SALAS_POR_NOMBRE[nombre]
    return parser_sala


def _detect_seccion(head: str, id_cendoj: str | None) -> int | None:
    match = _SECCION_LABEL_RE.search(head)
    if match:
        return int(match.group(1))
    if id_cendoj:
        seccion = int(id_cendoj[7:10])
        if seccion:
            return seccion
    match = _SECCION_ORDINAL_RE.search(head)
    if not match:
        return None
    if match.group(1):
        return _SECCIONES[_strip_accents(match.group(1)).lower()]
    return int(match.group(2))


def _detect_tipo_recurso(text: str) -> Literal["casacion", "otro"] | None:
    """Classify the appeal, trusting the header "Procedimiento" field first.

    "Casación para la unificación de doctrina" and "en interés de la ley" are
    different (pre-2016) appeals and count as "otro".
    """
    match = _PROCEDIMIENTO_RE.search(text)
    if match:
        value = _NEXT_LABEL_RE.split(match.group(1))[0]
        if re.search(r"casaci[oó]n", value, re.IGNORECASE):
            return "otro" if _UNIFICACION_RE.search(value) else "casacion"
        return "otro"
    for casacion in _CASACION_RE.finditer(text):
        if not _UNIFICACION_RE.match(text, _skip_filler(text, casacion.end())):
            return "casacion"
    if _CASACION_RE.search(text) or _OTRO_RECURSO_RE.search(text):
        return "otro"
    return None


def _skip_filler(text: str, position: int) -> int:
    """Skip "para la" / "en" between "casación" and its qualifier."""
    match = re.compile(r"\s*(?:para\s+la\s+|en\s+)?", re.IGNORECASE).match(text, position)
    return match.end() if match else position


def _first_number(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return _normalise_number(match.group(1), match.group(2))
    return None


def _normalise_number(number: str, year: str) -> str:
    digits = number.replace(".", "").lstrip("0") or "0"
    return f"{digits}/{year}"


# ---------------------------------------------------------------------------
# Admission question and doctrine passage
# ---------------------------------------------------------------------------


def _extract_cuestion(text: str) -> str | None:
    """Extract the question that the admission order found of cassation interest."""
    for match in _CUESTION_RE.finditer(text):
        end = _next_heading(text, match.end())
        stop = _CUESTION_STOP_RE.search(text, match.end(), end)
        if stop:
            end = stop.start()
        lines = text[match.end() : end].split("\n")
        kept: list[str] = []
        for line in lines:
            # A question may be split into a lead-in plus enumerated items;
            # anything else after the first full paragraph is unrelated text.
            if kept and len(" ".join(kept)) >= 40 and not _LIST_ITEM_RE.match(line):
                break
            kept.append(line)
        passage = _clean_passage(" ".join(kept))
        if len(passage) >= 20:
            return _cap(passage)
    return None


def _extract_doctrina(text: str) -> tuple[bool, str | None]:
    """Locate the passage where the Court states its own doctrine.

    Three signals, from the most to the least precise passage:

    1. an operative phrase ("fijamos la siguiente doctrina", "la respuesta a
       la cuestión ... debe ser", ...): the passage starts at that sentence;
    2. a legal ground whose title announces the interpretation ("Criterios
       interpretativos", "Contenido interpretativo de esta sentencia", ...):
       the passage is the body of that ground;
    3. a FALLO that fixes "los criterios interpretativos expresados en el
       fundamento jurídico N": the passage is that ground, if it can be found.
    """
    fundamentos = _FUNDAMENTOS_RE.search(text)
    start = fundamentos.end() if fundamentos else 0

    phrase = _DOCTRINE_PHRASE_RE.search(text, start)
    if phrase:
        begin = _sentence_start(text, phrase.start())
        passage = _clean_passage(text[begin : _next_heading(text, phrase.end())])
        return True, _cap(passage) or None

    for heading in _HEADING_RE.finditer(text, start):
        if not heading.group("ordinal"):
            continue
        title_end = text.find("\n", heading.end())
        title_end = min(
            len(text) if title_end == -1 else title_end,
            heading.end() + 250,
            _next_heading(text, heading.end()),
        )
        title = _DOCTRINE_HEADING_RE.search(text, heading.end(), title_end)
        if not title:
            continue
        body = _clean_passage(text[title_end : _next_heading(text, title_end)])
        if len(body) < 40:
            # Unbroken text: the title and the body share the line.
            body = _clean_passage(text[title.end() : _next_heading(text, title.end())])
        return True, _cap(body) or None

    fallo = _FALLO_CRITERIOS_RE.search(text, start)
    if fallo:
        return True, _referenced_ground(text, start, fallo.group("resto"))
    return False, None


def _referenced_ground(text: str, start: int, resto: str) -> str | None:
    """Return the body of the ground cited as "fundamento jurídico <N>"."""
    ref = _FUNDAMENTO_REF_RE.search(resto)
    if not ref:
        return None
    raw = _strip_accents(ref.group("ref")).upper()
    if raw.isdigit():
        index = int(raw) - 1
        if not 0 <= index < len(_ORDINALES):
            return None
        raw = _ORDINALES[index]
    for heading in _HEADING_RE.finditer(text, start):
        if heading.group("fallo"):
            break
        ordinal = heading.group("ordinal")
        if ordinal and _strip_accents(ordinal) == raw:
            body = _clean_passage(text[heading.end() : _next_heading(text, heading.end())])
            return _cap(body) or None
    return None


def _next_heading(text: str, position: int) -> int:
    match = _HEADING_RE.search(text, position)
    return match.start() if match else len(text)


def _sentence_start(text: str, position: int) -> int:
    line_start = text.rfind("\n", 0, position) + 1
    start = line_start
    for match in _SENTENCE_END_RE.finditer(text, line_start, position):
        start = match.end()
    return start


def _clean_passage(passage: str) -> str:
    cleaned = re.sub(r"\s+", " ", passage).strip()
    return cleaned.lstrip(".:;,-–— ").strip()


def _cap(passage: str, limit: int = MAX_PASSAGE_CHARS) -> str:
    """Trim ``passage`` to ``limit`` characters, ending at a sentence if possible."""
    if len(passage) <= limit:
        return passage
    window = passage[:limit]
    cut = 0
    for match in _SENTENCE_END_RE.finditer(window):
        cut = match.end()
    if cut >= limit // 3:
        return window[:cut].strip()
    space = window.rfind(" ")
    return window[: space if space > 0 else limit].rstrip(" ,;:") + "…"
