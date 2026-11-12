"""Verbatim copies of the parsers as they were BEFORE their format knowledge became spec data.

Oracles for test_formatos_specs.py: spec v1 must reproduce exactly what these return.
Taken from the repository history; do not "fix" them.
"""

# ruff: noqa
# fmt: off
# mypy: ignore-errors
from __future__ import annotations

from datetime import UTC, date, datetime
import re
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import parsel

from hacienda_gpt.crawler.boletin_autonomico import RegionalBoletinSpec, _first_text
from hacienda_gpt.crawler.teac import _first_match, _normalise_sentido, _normalise_vinculante
from hacienda_gpt.proactive.impacto import MAX_NOTA, MAX_TEXTO, ArticuloModificado, _lineas
from hacienda_gpt.proactive.novedades import SECCION_GENERAL, SECCIONES_POR_TITULO, Analisis, ItemSumario, NormaModificada
from hacienda_gpt.tax.fuentes import norma_de_parametros
from hacienda_gpt.tax.vigencia_normas import EstadoNorma


def items_del_sumario(payload: Mapping[str, Any], fecha: date) -> list[ItemSumario]:
    """Flatten one day's summary (the JSON of ``/datosabiertos/api/boe/sumario/<día>``)."""

    def lista(value: Any) -> list[Any]:
        return value if isinstance(value, list) else [] if value is None else [value]

    out: list[ItemSumario] = []

    def recorrer(node: Any, seccion: str, departamento: str, epigrafe: str) -> None:
        if not isinstance(node, dict):
            return
        for item in lista(node.get("item")):
            if isinstance(item, dict) and item.get("identificador") and item.get("titulo"):
                out.append(
                    ItemSumario(
                        boe_id=str(item["identificador"]),
                        fecha=fecha,
                        seccion=seccion,
                        departamento=departamento,
                        epigrafe=epigrafe,
                        titulo=" ".join(str(item["titulo"]).split()),
                        url=str(item.get("url_html") or ""),
                    )
                )
        for sub in lista(node.get("epigrafe")):
            if isinstance(sub, dict):
                recorrer(sub, seccion, departamento, str(sub.get("nombre") or epigrafe))
        for sub in lista(node.get("texto")):
            recorrer(sub, seccion, departamento, epigrafe)

    sumario = (payload.get("data") or {}).get("sumario") or {}
    for diario in lista(sumario.get("diario")):
        for seccion in lista(diario.get("seccion") if isinstance(diario, dict) else None):
            codigo = str(seccion.get("codigo") or "")
            if codigo != SECCION_GENERAL and codigo not in SECCIONES_POR_TITULO:
                continue
            for departamento in lista(seccion.get("departamento")):
                if isinstance(departamento, dict):
                    recorrer(departamento, codigo, str(departamento.get("nombre") or ""), "")
    return out


def leer_analisis(xml: str | bytes, vigiladas: frozenset[str] = frozenset()) -> Analisis:
    """Parse the BOE's XML of a disposition (``/diario_boe/xml.php?id=…``)."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return Analisis()

    def texto(path: str) -> str:
        node = root.find(path)
        return " ".join((node.text or "").split()) if node is not None else ""

    modifica = []
    for anterior in root.iterfind(".//analisis/referencias/anteriores/anterior"):
        boe_id = anterior.get("referencia") or ""
        if not boe_id:
            continue
        palabra, detalle = anterior.find("palabra"), anterior.find("texto")
        modifica.append(
            NormaModificada(
                boe_id=boe_id,
                accion=" ".join((palabra.text or "").split()) if palabra is not None else "",
                texto=" ".join((detalle.text or "").split()) if detalle is not None else "",
                vigilada=boe_id in vigiladas,
                parametros=norma_de_parametros(boe_id) is not None,
            )
        )
    return Analisis(
        rango=texto(".//metadatos/rango"),
        origen=texto(".//metadatos/origen_legislativo"),
        departamento=texto(".//metadatos/departamento"),
        materias=[" ".join((m.text or "").split()) for m in root.iterfind(".//analisis/materias/materia") if m.text],
        modifica=modifica,
    )



def estado_desde_xml(boe_id: str, xml: str | bytes, *, hoy: date | None = None) -> EstadoNorma:
    """Read validation / repeal from the BOE's XML of the disposition (``/diario_boe/xml.php?id=…``)."""
    root = ElementTree.fromstring(xml)

    def texto(path: str) -> str:
        node = root.find(path)
        return " ".join((node.text or "").split()) if node is not None else ""

    posteriores = [
        (" ".join((p.findtext("palabra") or "").split()), " ".join((p.findtext("texto") or "").split()))
        for p in root.iterfind(".//analisis/referencias/posteriores/posterior")
    ]
    derogacion = next(((p, t) for p, t in posteriores if p.upper().startswith("SE DEROGA")), None)
    if texto(".//metadatos/estatus_derogacion") == "S" or derogacion is not None:
        detalle = " ".join(derogacion) if derogacion else f"derogada el {texto('.//metadatos/fecha_derogacion')}"
        return EstadoNorma(boe_id=boe_id, estado="derogada", detalle=detalle, comprobado_el=hoy)
    convalidacion = next(((p, t) for p, t in posteriores if "convalidaci" in p.lower()), None)
    if convalidacion is not None:
        return EstadoNorma(boe_id=boe_id, estado="convalidada", detalle=" ".join(convalidacion), comprobado_el=hoy)
    return EstadoNorma(boe_id=boe_id, estado="pendiente", comprobado_el=hoy)



# A block we report: an article or an additional/transitory/derogatory/final provision.
_HEADING_RE = re.compile(
    r"^(?:Art[ií]culo\s+(?:\d+(?:\s+(?:bis|ter|qu[aá]ter|quinquies|sexies|septies|octies|nonies|decies))?"
    r"|[úu]nico|primero|segundo|tercero|cuarto|quinto)\s*\."
    r"|Disposici[oó]n\s+(?:adicional|transitoria|derogatoria|final)\b)",
    re.IGNORECASE,
)
# Structural headings that end the previous block without starting a reported one.
_BOUNDARY_RE = re.compile(
    r"^(?:\[Bloque\b|T[IÍ]TULO\b|CAP[IÍ]TULO\b|Secci[oó]n\s|Subsecci[oó]n\s|LIBRO\b|ANEXO\b|PRE[AÁ]MBULO\b"
    r"|EXPOSICI[OÓ]N DE MOTIVOS\b)"
)
_FECHA = r"(\d{1,2})/(\d{1,2})/(\d{4})"
# "Última actualización, publicada el 31/12/2020, en vigor a partir del 01/01/2021."
# "Texto añadido, publicado el …", "Modificación publicada el …", "Texto original, publicado el …"
_VERSION_RE = re.compile(
    rf"publicad[oa]\s+el\s+{_FECHA}(?:\s*,?\s*en\s+vigor\s+a\s+partir\s+del?\s+{_FECHA})?", re.IGNORECASE
)
_NOTA_RE = re.compile(
    r"^Se\s+(?:modifica|modifican|a[ñn]ade|a[ñn]aden|deroga|derogan|suprime|suprimen|renumera|sustituye|declara|"
    r"deja|dejan|corrige|corrigen|actualiza|actualizan|prorroga|introduce)\b",
    re.IGNORECASE,
)
# Labels of the version selector, not part of the wording.
_UI_RE = re.compile(r"^(?:Seleccionar redacci[oó]n|Redacci[oó]n\b|Subir\b|\[Bloque\b)", re.IGNORECASE)


def _date(day: str, month: str, year: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def articulos_modificados(html: str, *, desde: date) -> list[ArticuloModificado]:
    """Articles and provisions of a BOE consolidated law whose latest wording was published after ``desde``.

    Works from the text, not the markup: a block starts at a line beginning with
    "Artículo N." or "Disposición adicional/transitoria/derogatoria/final …" and
    its latest wording is the most recent "publicada el DD/MM/YYYY" line inside it.
    """
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in _lineas(html):
        if _HEADING_RE.match(line) and not _VERSION_RE.search(line):
            current = []
            blocks.append((line, current))
        elif _BOUNDARY_RE.match(line):
            current = None
        elif current is not None:
            current.append(line)

    out: list[ArticuloModificado] = []
    for titulo, lines in blocks:
        latest: tuple[date, date | None] | None = None
        notas: list[str] = []
        texto: list[str] = []
        for line in lines:
            version = _VERSION_RE.search(line)
            if version:
                publicado = _date(*version.group(1, 2, 3))
                if publicado and (latest is None or publicado > latest[0]):
                    latest = (publicado, _date(*version.group(4, 5, 6)) if version.group(4) else None)
            elif _NOTA_RE.match(line):
                notas.append(line)
            elif not _UI_RE.match(line):
                texto.append(line)
        if latest is None or latest[0] <= desde:
            continue
        out.append(
            ArticuloModificado(
                titulo=titulo,
                publicado=latest[0],
                en_vigor=latest[1],
                nota=" ".join(notas)[:MAX_NOTA],
                texto="\n".join(texto)[:MAX_TEXTO],
            )
        )
    return out



DYCTEA_NOT_FOUND_MARKER = "El criterio no existe"

# Labels are matched in lowercase against the visible text immediately preceding
# the value. Multiple aliases let us cope with small wording differences across
# resolution layouts (TEAC vs TEAR, vinculante vs no vinculante, etc.).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "numero_resolucion": ("número de resolución", "numero de resolucion", "nº resolución", "n.º resolución"),
    "sala": ("sala", "unidad resolutoria"),
    "fecha_resolucion": ("fecha de la resolución", "fecha resolución", "fecha de resolucion"),
    "sentido": ("sentido", "fallo"),
    "vinculante": ("vinculante", "criterio vinculante"),
    "concepto": ("concepto", "concepto tributario"),
    "materia": ("materia",),
    "norma": ("norma",),
    "precepto": ("precepto",),
    "criterio": ("criterio",),
}

_NUM_RESOLUCION_PATTERN = re.compile(r"\b\d{2}/\d{4,6}/\d{4}(?:/\d{2}/\d{2})?\b")
_DATE_PATTERN = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")


def parse_teac_criterio(html: str, *, criterio_id: int | str | None = None) -> dict[str, Any] | None:
    """Parse a DYCTEA resolution detail page into a structured dict.

    Returns None when the page is the "criterio no existe" error placeholder,
    so callers can short-circuit storage.
    """
    if DYCTEA_NOT_FOUND_MARKER in html:
        return None

    selector = parsel.Selector(text=html)
    fields: dict[str, str | None] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        fields[canonical] = _extract_field(selector, aliases)

    numero = fields.get("numero_resolucion") or _first_match(_NUM_RESOLUCION_PATTERN, html)
    fecha = _parse_date(fields.get("fecha_resolucion") or "")

    return {
        "source_authority": "TEAC",
        "criterio_id": criterio_id,
        "numero_resolucion": numero,
        "sala": fields.get("sala"),
        "fecha_resolucion": fecha.isoformat() if fecha else None,
        "sentido": _normalise_sentido(fields.get("sentido")),
        "vinculante": _normalise_vinculante(fields.get("vinculante")),
        "concepto": fields.get("concepto"),
        "materia": fields.get("materia"),
        "norma": fields.get("norma"),
        "precepto": fields.get("precepto"),
        "criterio": fields.get("criterio"),
        "ingested_at": datetime.now(UTC).isoformat(),
    }


def _extract_field(selector: Any, aliases: tuple[str, ...]) -> str | None:
    """Look up a labelled value tolerating dl/dt/dd, table tr/td and label/span layouts."""
    for alias in aliases:
        # dt/dd pattern (definition list)
        value = selector.xpath(
            f"//dt[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}']/following-sibling::dd[1]//text()"
        ).getall()
        if value:
            joined = " ".join(part.strip() for part in value if part.strip())
            if joined:
                return joined
        # tr/td pattern
        value = (
            selector.xpath(
                f"//th[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}']/following-sibling::td[1]//text()"
            ).getall()
            or selector.xpath(
                f"//td[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}']/following-sibling::td[1]//text()"
            ).getall()
            or selector.xpath(
                f"//td[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}:']/following-sibling::td[1]//text()"
            ).getall()
        )
        if value:
            joined = " ".join(part.strip() for part in value if part.strip())
            if joined:
                return joined
        # label + span/strong pattern
        value = selector.xpath(
            f"//*[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}:']/following-sibling::*[1]//text()"
        ).getall()
        if value:
            joined = " ".join(part.strip() for part in value if part.strip())
            if joined:
                return joined
    return None



def _parse_date(value: str) -> date | None:
    match = _DATE_PATTERN.search(value)
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None



def parse_listing(spec: RegionalBoletinSpec, html: str) -> list[dict[str, Any]]:
    """Apply a spec to a result-listing HTML and return structured rows.

    Each row carries the link resolved to an absolute URL so the spider
    can directly enqueue follow-up downloads.
    """
    selector = parsel.Selector(text=html)
    rows: list[dict[str, Any]] = []
    for item in selector.xpath(spec.item_xpath):
        title = _first_text(item.xpath(spec.title_xpath))
        date_value = _first_text(item.xpath(spec.date_xpath))
        link = _first_text(item.xpath(spec.link_xpath))
        if not link:
            continue
        rows.append(
            {
                "title": title,
                "date_raw": date_value,
                "link": urljoin(spec.base_url, link.strip()),
                "boletin": spec.key,
                "ccaa_ine": spec.ccaa_ine,
            }
        )
    return rows



# --------------------------------------------------------------------------- #
# CENDOJ document (hacienda_gpt/crawler/cendoj.py before the ``cendoj_documento`` spec).
# Names prefixed with ``_cendoj`` only to coexist with the copies above.
# --------------------------------------------------------------------------- #

_CENDOJ_STRONG_TAX_KEYWORDS = (
    "irpf",
    "iva",
    "impuesto sobre sociedades",
    "impuesto sobre el valor añadido",
    "impuesto sobre la renta",
    "impuesto sobre sucesiones",
    "impuesto sobre transmisiones",
    "ley general tributaria",
    "lgt",
    "agencia tributaria",
    "aeat",
    "obligación tributaria",
    "deuda tributaria",
)
_CENDOJ_WEAK_TAX_KEYWORDS = (
    "tributario",
    "tributaria",
    "tributo",
    "tributos",
    "fiscal",
    "hacienda",
    "liquidación",
    "sanción",
    "sancionador",
)

_CENDOJ_ROJ_PATTERN = re.compile(r"\bROJ\s*:\s*(STS\s*\d+/\d{4})\b", re.IGNORECASE)
_CENDOJ_ECLI_PATTERN = re.compile(r"\bECLI\s*:\s*([A-Z]{2}:[A-Z]+:\d{4}:\d+(?::[A-Z])?)\b", re.IGNORECASE)
_CENDOJ_DATE_PATTERN = re.compile(r"\b(\d{2})[/-](\d{2})[/-](\d{4})\b")
_CENDOJ_SALA_PATTERN = re.compile(r"Sala\s+(Tercera|3[ªa\.]+|de\s+lo\s+Contencioso[\w\s\-]*)", re.IGNORECASE)


def parse_cendoj_document(html: str, *, source_url: str | None = None) -> dict[str, Any]:
    selector = parsel.Selector(text=html)
    body_text = " ".join(selector.xpath("//body//text()").getall())
    cleaned = re.sub(r"\s+", " ", body_text).strip()

    roj = _cendoj_first_match(_CENDOJ_ROJ_PATTERN, cleaned)
    if roj:
        roj = re.sub(r"\s+", " ", roj).strip()
    ecli = _cendoj_first_match(_CENDOJ_ECLI_PATTERN, cleaned)
    fecha = _cendoj_extract_first_date(cleaned)
    ponente = _cendoj_extract_after_label(cleaned, "Ponente")
    sala = _cendoj_normalise_sala(cleaned)

    return {
        "source_authority": (
            "TS_SALA_TERCERA"
            if "sala tercera" in (sala or "").lower() or "contencioso" in (sala or "").lower()
            else "TS"
        ),
        "source_hierarchy_rank": 10,
        "tribunal": "Tribunal Supremo",
        "sala": sala,
        "roj": roj,
        "ecli": ecli,
        "fecha_resolucion": fecha.isoformat() if fecha else None,
        "ponente": ponente,
        "source_url": source_url,
        "is_tributario": _cendoj_classify_tributario(cleaned),
        "tributos_detectados": _cendoj_detect_tributos(cleaned),
        "ingested_at": datetime.now(UTC).isoformat(),
    }


def _cendoj_classify_tributario(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in lowered for keyword in _CENDOJ_STRONG_TAX_KEYWORDS):
        return True
    weak_hits = sum(1 for keyword in _CENDOJ_WEAK_TAX_KEYWORDS if keyword in lowered)
    return weak_hits >= 2


def _cendoj_detect_tributos(text: str) -> list[str]:
    lowered = text.lower()
    detected: list[str] = []
    catalog: list[tuple[str, list[tuple[str, bool]]]] = [
        ("IRPF", [("irpf", True), ("impuesto sobre la renta de las personas físicas", False)]),
        ("IVA", [("iva", True), ("impuesto sobre el valor añadido", False)]),
        ("IS", [("impuesto sobre sociedades", False), ("is", True), ("lis", True)]),
        ("ISD", [("isd", True), ("impuesto sobre sucesiones", False)]),
        ("ITP_AJD", [("itp", True), ("ajd", True), ("impuesto sobre transmisiones patrimoniales", False)]),
        ("IBI", [("ibi", True), ("impuesto sobre bienes inmuebles", False)]),
        ("IAE", [("iae", True), ("impuesto sobre actividades económicas", False)]),
    ]
    for code, aliases in catalog:
        for alias, needs_boundary in aliases:
            if needs_boundary:
                if re.search(rf"\b{re.escape(alias)}\b", lowered):
                    detected.append(code)
                    break
            elif alias in lowered:
                detected.append(code)
                break
    return detected


def _cendoj_normalise_sala(text: str) -> str | None:
    match = _CENDOJ_SALA_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(0)
    if "tercera" in raw.lower() or "3" in raw:
        return "Sala Tercera (Contencioso-Administrativo)"
    if "contencioso" in raw.lower():
        return "Sala de lo Contencioso-Administrativo"
    return raw.strip()


def _cendoj_extract_first_date(text: str) -> date | None:
    match = _CENDOJ_DATE_PATTERN.search(text)
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _cendoj_extract_after_label(text: str, label: str) -> str | None:
    pattern = re.compile(rf"{re.escape(label)}\s*:?\s*([^\n.]{{1,80}})", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().rstrip(".,;")
    value = re.split(r"\s{2,}|\s—\s|\s-\s", value)[0]
    return value.strip() or None


def _cendoj_first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None



# --------------------------------------------------------------------------- #
# AEAT sede: links the web crawler followed (hacienda_gpt/crawler/crawlers.py before the ``aeat_sede`` spec).
# --------------------------------------------------------------------------- #

_AEAT_ALLOWED_PATHS = [
    r"/Sede/irpf",
    r"/Sede/iva",
    r"/Sede/no-residentes",
    r"/Sede/censos-nif-domicilio-fiscal",
    r"/Sede/colaborar-agencia-tributaria/modelos",
    r"/Sede/recaudacion/aplazamientos-fraccionamientos-deudas-tributarias",
    r"/Sede/colaborar-agencia-tributaria/calendario-contribuyente",
    r"/Sede/ayuda/manuales-videos-folletos",
    r"/Sede/informacion-administrativa/sanciones",
    r"/Sede/declaraciones-informativas",
    r"/Sede/declaracion-informativa",
]


def aeat_links_followed(url: str, html: str) -> list[str]:
    """The URLs ``AgenciaTributariaWebCrawler._follow_domain_links`` requested from one page, in order."""
    from scrapy.http import HtmlResponse
    from scrapy.linkextractors import LinkExtractor

    response = HtmlResponse(url=url, body=html.encode("utf-8"), encoding="utf-8")
    allowed_patterns = [re.compile(path) for path in _AEAT_ALLOWED_PATHS]
    link_extractor = LinkExtractor(allow=allowed_patterns, allow_domains=["sede.agenciatributaria.gob.es"])
    return [link.url for link in link_extractor.extract_links(response)]



# --------------------------------------------------------------------------- #
# TEAC listing (hacienda_gpt/cli/teac_seed.py before the ``teac_listado`` spec).
# --------------------------------------------------------------------------- #

_TEAC_CRITERIO_LINK_RE = re.compile(r"""href=['"](criterio\.aspx\?id=([^'"&]+))[^'"]*['"]""", re.IGNORECASE)


def teac_ids_de_pagina(text: str) -> list[str]:
    """The ids ``collect_criterio_ids`` took from one listing page, in order (repeats included)."""
    return [match.group(2) for match in _TEAC_CRITERIO_LINK_RE.finditer(text)]



# --------------------------------------------------------------------------- #
# DGT search results and document (hacienda_gpt/cli/dgt_seed.py before the ``dgt_*`` specs).
# --------------------------------------------------------------------------- #

_DGT_DOC_ID_RE = re.compile(r'id="doc_(\d+)"')
_DGT_TOTAL_PAGES_RE = re.compile(r'id="total_pages">(\d+)<')
_DGT_NUM_CONSULTA_IN_DOC_RE = re.compile(r'class="NUM-CONSULTA"[^>]*>([^<]+)</p>')
_DGT_SEARCH_ROW_RE = re.compile(
    r'id="doc_(\d+)"[^>]*>'  # capture doc_id
    r".*?"  # any HTML in between
    r'class="NUM-CONSULTA"[^>]*>'  # the NUM-CONSULTA span
    r"\s*<strong>\s*"
    r"([VN]\d{4}-\d{2})"  # capture V0058-25 / N1234-23 only
    r"\b",  # word boundary — stops at extra text
    re.DOTALL,
)
_DGT_CANONICAL_NUM_RE = re.compile(r"\b([VN]\d{4}-\d{2})\b")


def dgt_resultados(html: str) -> tuple[list[tuple[str, str]], int, str]:
    """(rows, total_pages, query) as ``_search_month`` computed them from the response."""
    rows = _DGT_SEARCH_ROW_RE.findall(html)
    if not rows:
        rows = [(doc_id, "") for doc_id in _DGT_DOC_ID_RE.findall(html)]

    total_pages_match = _DGT_TOTAL_PAGES_RE.search(html)
    total_pages = int(total_pages_match.group(1)) if total_pages_match else 1

    query_match = re.search(r'id="query"[^>]*value="([^"]*)"', html)
    query = query_match.group(1) if query_match else ""
    return rows, total_pages, query


def dgt_parse_doc_num(html: str) -> str | None:
    m = _DGT_NUM_CONSULTA_IN_DOC_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip()
    canonical = _DGT_CANONICAL_NUM_RE.search(raw)
    return canonical.group(1) if canonical else raw
