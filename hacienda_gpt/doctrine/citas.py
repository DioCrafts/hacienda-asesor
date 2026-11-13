"""Extraction of citations between tax-law sources, and document identity.

Everything here is deterministic. A citation is a reference, inside one
document, to another ruling or consulta, reduced to a canonical key:

* ``DGT:V1234-20``            binding consulta of the Dirección General de Tributos
* ``TEAC:00/01234/2017``      resolution of the TEAC, by "RG" number
* ``TS:ECLI:ES:TS:2021:1234`` Supreme Court ruling by ECLI
* ``TS:ROJ:1234/2021``        … by CENDOJ ROJ
* ``TS:REC:1234/2019``        … by appeal number
* ``TS:NUM:1456/2021``        … by judgment number
* ``TC:182/2021``             Constitutional Court ruling

One Supreme Court ruling can be reached by several keys; the graph stores them
as aliases of a single node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any

MONTHS = {
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
_MONTH_ALT = "|".join(MONTHS)

# Authority rank: who prevails when two sources disagree (higher wins).
RANGO: dict[str, int] = {"TC": 5, "TS": 4, "TEAC": 3, "DGT": 2, "AEAT": 1}

_DGT_RE = re.compile(r"\b(V\d{4}-\d{2})\b")
_TEAC_RG_RE = re.compile(r"\b(?:R\.?\s?G\.?\s*:?\s*)?(\d{2})\s*/\s*(\d{3,5})\s*/\s*((?:19|20)\d{2})\b")
_ECLI_RE = re.compile(r"\bECLI\s*:\s*(ES:TS:\d{4}:\d+)\b", re.IGNORECASE)
_ROJ_RE = re.compile(r"\bROJ\s*:?\s*STS\s+(\d{1,5})/(\d{4})\b", re.IGNORECASE)
_STC_RE = re.compile(
    r"\b(?:STC|sentencia\s+del\s+Tribunal\s+Constitucional(?:\s+n[úu]m(?:ero)?\.?)?)\s+(\d{1,3})/((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
# "sentencia del Tribunal Supremo de 3 de junio de 2020 (rec. 1234/2019)", "STS de 3-6-2020, recurso de casación
# núm. 1234/2019", "STS 1456/2021, de 3 de diciembre (rec. cas. 1234/2019)"…
_TS_MENTION_RE = re.compile(
    r"\b(?:SSTS|STS|sentencias?\s+del\s+Tribunal\s+Supremo|sentencias?\s+de\s+la\s+Sala\s+Tercera"
    r"|el\s+Tribunal\s+Supremo,?\s+en\s+(?:su\s+)?sentencias?)\b",
    re.IGNORECASE,
)
_TS_NUM_RE = re.compile(
    r"\b(?:n[úu]m(?:ero)?\.?|n\.?[ºo])\s*(\d{1,2}\.?\d{3}|\d{1,4})/((?:19|20)\d{2})\b", re.IGNORECASE
)
_TS_BARE_NUM_RE = re.compile(r"^\W{0,3}(\d{1,2}\.?\d{3}|\d{1,4})/((?:19|20)\d{2})\b")
_TS_REC_RE = re.compile(
    r"\b(?:rec(?:urso)?s?\.?(?:\s+de)?(?:\s+cas(?:aci[óo]n)?\.?)?|RCA|RC)\s*(?:n[úu]m(?:ero)?\.?|n\.?[ºo])?\s*:?\s*/?\s*"
    r"(\d{1,5})/((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_DATE_WORDS_RE = re.compile(rf"\b(\d{{1,2}})\s+de\s+({_MONTH_ALT})\s+de\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-]((?:19|20)\d{2})\b")

# How far after "STS"/"sentencia del Tribunal Supremo" its identifiers may appear.
_TS_WINDOW = 220
_CONTEXT = 260


@dataclass(frozen=True)
class Cita:
    """A reference found in a document."""

    clave: str  # canonical key of the cited document
    tipo: str  # DGT | TEAC | TS | TC
    texto: str  # the matched reference
    contexto: str  # surrounding passage, for a reader who needs the wider picture
    frase: str = ""  # the single sentence holding the reference: what is said about THIS document
    fecha: date | None = None  # date of the cited document, when stated
    alias: tuple[str, ...] = ()  # other keys of the same cited document


@dataclass
class Documento:
    """Identity of a corpus document as a node of the doctrine graph."""

    clave: str
    tipo: str
    alias: list[str] = field(default_factory=list)
    fecha: date | None = None
    titulo: str = ""
    locator: str = ""
    vinculante: bool | None = None
    fija_doctrina: bool = False


def parse_date(text: str) -> date | None:
    """First date in ``text`` ("3 de junio de 2020", "03/06/2020", ISO)."""
    iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)", text)
    words = _DATE_WORDS_RE.search(text)
    numeric = _DATE_NUM_RE.search(text)
    candidates: list[tuple[int, tuple[int, int, int]]] = []
    if iso:
        candidates.append((iso.start(), (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))))
    if words:
        candidates.append((words.start(), (int(words.group(3)), MONTHS[words.group(2).lower()], int(words.group(1)))))
    if numeric:
        candidates.append((numeric.start(), (int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))))
    for _, (year, month, day) in sorted(candidates):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _number(raw: str) -> str:
    return str(int(raw.replace(".", "")))


def teac_key(sala: str, numero: str, anio: str) -> str:
    return f"TEAC:{sala}/{int(numero):05d}/{anio}"


def _context(text: str, start: int, end: int) -> str:
    return " ".join(text[max(0, start - _CONTEXT) : end + _CONTEXT].split())


def _around(text: str, start: int, end: int) -> tuple[str, str]:
    """(wide context, sentence) for a reference at ``[start, end)``."""
    return _context(text, start, end), _sentence(text, start, end)


# Sentence boundary: closing punctuation followed by a capitalised start. Abbreviations
# inside references ("rec. cas. 1234", "núm. 45", "art. 38") are followed by lowercase
# or digits, so they do not split.
_SENTENCE_END_RE = re.compile(r"(?<=[.;:])\s+(?=[A-ZÁÉÍÓÚÑ¿¡«\"(])|\n+")


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence of ``text`` that contains ``[start, end)``."""
    left = 0
    for boundary in _SENTENCE_END_RE.finditer(text, 0, start):
        left = boundary.end()
    right_match = _SENTENCE_END_RE.search(text, end)
    right = right_match.start() if right_match else len(text)
    return " ".join(text[left:right].split())


def extract_citations(text: str, *, propias: set[str] | None = None) -> list[Cita]:
    """Citations in ``text``, deduplicated by key, skipping the document's own keys."""
    propias = propias or set()
    found: dict[str, Cita] = {}

    def add(cita: Cita) -> None:
        if cita.clave in propias or any(alias in propias for alias in cita.alias):
            return
        found.setdefault(cita.clave, cita)

    for match in _DGT_RE.finditer(text):
        add(Cita(f"DGT:{match.group(1)}", "DGT", match.group(0), *_around(text, match.start(), match.end())))

    for match in _STC_RE.finditer(text):
        clave = f"TC:{int(match.group(1))}/{match.group(2)}"
        add(Cita(clave, "TC", match.group(0), *_around(text, match.start(), match.end())))

    # A bare "00/1234/2017" is only a TEAC reference when the passage says so.
    for match in _TEAC_RG_RE.finditer(text):
        window = text[max(0, match.start() - 160) : match.end() + 40].lower()
        if not re.search(r"\bteac\b|tribunal\s+econ[óo]mico|\br\.?\s?g\.?\b|resoluci[óo]n", window):
            continue
        add(
            Cita(
                teac_key(match.group(1), match.group(2), match.group(3)),
                "TEAC",
                match.group(0),
                *_around(text, match.start(), match.end()),
                fecha=parse_date(text[max(0, match.start() - 120) : match.end()]),
            )
        )

    for match in _ECLI_RE.finditer(text):
        clave = f"TS:ECLI:{match.group(1).upper()}"
        add(Cita(clave, "TS", match.group(0), *_around(text, match.start(), match.end())))
    for match in _ROJ_RE.finditer(text):
        clave = f"TS:ROJ:{int(match.group(1))}/{match.group(2)}"
        add(Cita(clave, "TS", match.group(0), *_around(text, match.start(), match.end())))

    # Supreme Court rulings cited in prose: gather the identifiers stated right
    # after the mention (appeal number is the most stable one across sources).
    mentions = list(_TS_MENTION_RE.finditer(text))
    for index, match in enumerate(mentions):
        # Identifiers belong to this mention only up to the next one.
        limit = mentions[index + 1].start() if index + 1 < len(mentions) else len(text)
        window = text[match.end() : min(match.end() + _TS_WINDOW, limit)]
        keys: list[str] = []
        recurso = _TS_REC_RE.search(window)
        if recurso:
            keys.append(f"TS:REC:{int(recurso.group(1))}/{recurso.group(2)}")
        # The judgment number either follows the mention directly ("STS 1.456/2021")
        # or is introduced by "núm." — but "recurso núm. X" is the appeal, not the judgment.
        numero = _TS_BARE_NUM_RE.search(window)
        if numero is None:
            numero = next(
                (
                    m
                    for m in _TS_NUM_RE.finditer(window)
                    if not (recurso and recurso.start() <= m.start() <= recurso.end())
                ),
                None,
            )
        if numero:
            keys.append(f"TS:NUM:{_number(numero.group(1))}/{numero.group(2)}")
        if not keys:
            continue  # "el Tribunal Supremo" with nothing identifiable: not a usable citation
        end = match.end() + max(m.end() for m in (recurso, numero) if m)
        add(
            Cita(
                keys[0],
                "TS",
                " ".join(text[match.start() : end].split()),
                *_around(text, match.start(), end),
                fecha=parse_date(window),
                alias=tuple(keys[1:]),
            )
        )
    return list(found.values())


def _str(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def identify_document(metadata: dict[str, Any], text: str = "") -> Documento | None:
    """Graph identity of a corpus document from its chunk metadata.

    Returns None for documents that are not doctrine or case law (statutes,
    AEAT pages): they are cited, but they are not nodes whose validity the
    graph tracks.
    """
    source = " ".join(str(metadata.get(key) or "") for key in ("source_url", "source", "source_file"))
    authority = (_str(metadata, "source_authority") or "").upper()
    fecha = parse_date(_str(metadata, "fecha_resolucion", "fecha") or "")
    titulo = _str(metadata, "title") or ""
    locator = _str(metadata, "source_url", "source", "source_file") or ""
    vinculante = metadata.get("vinculante") if isinstance(metadata.get("vinculante"), bool) else None

    consulta = _str(metadata, "num_consulta") or (
        _DGT_RE.search(source).group(1) if "dgt" in source.lower() and _DGT_RE.search(source) else None  # type: ignore[union-attr]
    )
    if consulta and _DGT_RE.fullmatch(consulta):
        return Documento(
            f"DGT:{consulta}",
            "DGT",
            fecha=fecha,
            titulo=titulo or f"Consulta {consulta}",
            locator=locator,
            vinculante=True,
        )

    if authority == "TEAC" or "teac" in source.lower():
        raw = _str(metadata, "numero_resolucion") or ""
        match = _TEAC_RG_RE.search(raw) or _TEAC_RG_RE.search(source.replace("_", "/"))
        if match:
            clave = teac_key(match.group(1), match.group(2), match.group(3))
            return Documento(clave, "TEAC", fecha=fecha, titulo=titulo or clave, locator=locator, vinculante=vinculante)
        return None

    if authority.startswith("TS") or "cendoj" in source.lower() or "poderjudicial" in source.lower():
        keys: list[str] = []
        ecli = _str(metadata, "ecli")
        if ecli:
            keys.append(f"TS:ECLI:{ecli.upper()}")
        roj = re.search(r"(\d{1,5})/(\d{4})", _str(metadata, "roj") or "")
        if roj:
            keys.append(f"TS:ROJ:{int(roj.group(1))}/{roj.group(2)}")
        recurso = re.search(r"(\d{1,5})/(\d{4})", _str(metadata, "numero_recurso") or "")
        if recurso:
            keys.append(f"TS:REC:{int(recurso.group(1))}/{recurso.group(2)}")
        numero = re.search(r"(\d{1,5})/(\d{4})", _str(metadata, "numero_sentencia") or "")
        if numero:
            keys.append(f"TS:NUM:{int(numero.group(1))}/{numero.group(2)}")
        if not keys:
            return None
        return Documento(
            keys[0],
            "TS",
            alias=keys[1:],
            fecha=fecha,
            titulo=titulo or keys[0],
            locator=locator,
            fija_doctrina=metadata.get("fija_doctrina") is True,
        )

    if "jurisprudencia-tc" in source.lower() or authority == "TC":
        match = _STC_RE.search(f"{titulo} {text[:600]}")
        if match:
            clave = f"TC:{int(match.group(1))}/{match.group(2)}"
            return Documento(clave, "TC", fecha=fecha, titulo=titulo or clave, locator=locator)
    return None
