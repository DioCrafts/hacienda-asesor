"""Crawler for CENDOJ (Tribunal Supremo Sala 3ª, sección Tributario).

CENDOJ (Centro de Documentación Judicial) is the official jurisprudence
repository at ``poderjudicial.es/search``. The portal is JavaScript-heavy
and ROJ identifiers are not part of the public URL; each document is
served behind an opaque hash. We therefore expose two ingestion modes:

- **Manual URL list**: pass the list of result URLs scraped from the search
  UI (or exported from a saved query) via ``--urls`` / ``urls_file``. This
  is the most robust path and works without Playwright.
- **Search-driven**: programmatic submission of the advanced search form
  with a configurable query string (e.g. ``"tributario"``). Requires
  Playwright (already a project dependency) because the form posts via
  Ajax and depends on JavaScript-set tokens.

Each downloaded resolution is persisted as raw HTML plus a JSON sidecar
with parsed metadata (ROJ, ECLI, court, date, ponente, voto particular).
A keyword classifier flags ``is_tributario`` so downstream indexing can
filter by tributary jurisprudence.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Generator
from datetime import UTC, date, datetime
from typing import Any

import parsel
import scrapy
from scrapy.http import Response

CENDOJ_BASE_URL = "https://www.poderjudicial.es"
CENDOJ_DEFAULT_USER_AGENT = "hacienda-asesor/0.1 (research; contacto@hacienda-asesor.local)"

# Keywords used to flag a resolution as "tributario". Conservative: at least
# one strong tax keyword OR two weak ones must appear in the document text.
_STRONG_TAX_KEYWORDS = (
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
_WEAK_TAX_KEYWORDS = (
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

_ROJ_PATTERN = re.compile(r"\bROJ\s*:\s*(STS\s*\d+/\d{4})\b", re.IGNORECASE)
_ECLI_PATTERN = re.compile(r"\bECLI\s*:\s*([A-Z]{2}:[A-Z]+:\d{4}:\d+(?::[A-Z])?)\b", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"\b(\d{2})[/-](\d{2})[/-](\d{4})\b")
_PONENTE_PATTERN = re.compile(r"Ponente\s*:?\s*([^\n<]+)", re.IGNORECASE)
_SALA_PATTERN = re.compile(r"Sala\s+(Tercera|3[ªa\.]+|de\s+lo\s+Contencioso[\w\s\-]*)", re.IGNORECASE)


def parse_cendoj_document(html: str, *, source_url: str | None = None) -> dict[str, Any]:
    """Parse a CENDOJ document HTML into a structured metadata dict.

    Unlike DYCTEA there is no negative marker — even unknown URLs return
    structured pages — so the caller decides whether to keep the result
    based on the ``is_tributario`` flag.
    """
    selector = parsel.Selector(text=html)
    body_text = " ".join(selector.xpath("//body//text()").getall())
    cleaned = re.sub(r"\s+", " ", body_text).strip()

    roj = _first_match(_ROJ_PATTERN, cleaned)
    if roj:
        roj = re.sub(r"\s+", " ", roj).strip()
    ecli = _first_match(_ECLI_PATTERN, cleaned)
    fecha = _extract_first_date(cleaned)
    ponente = _extract_after_label(cleaned, "Ponente")
    sala = _normalise_sala(cleaned)

    return {
        "source_authority": "TS_SALA_TERCERA" if "sala tercera" in (sala or "").lower() or "contencioso" in (sala or "").lower() else "TS",
        "source_hierarchy_rank": 10,
        "tribunal": "Tribunal Supremo",
        "sala": sala,
        "roj": roj,
        "ecli": ecli,
        "fecha_resolucion": fecha.isoformat() if fecha else None,
        "ponente": ponente,
        "source_url": source_url,
        "is_tributario": _classify_tributario(cleaned),
        "tributos_detectados": _detect_tributos(cleaned),
        "ingested_at": datetime.now(UTC).isoformat(),
    }


def _classify_tributario(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in lowered for keyword in _STRONG_TAX_KEYWORDS):
        return True
    weak_hits = sum(1 for keyword in _WEAK_TAX_KEYWORDS if keyword in lowered)
    return weak_hits >= 2


def _detect_tributos(text: str) -> list[str]:
    """Tax-code detection using word boundaries for short acronyms.

    Short acronyms like "IS" must be matched with ``\\b`` to avoid false
    positives (e.g. inside "es", "fis", "país"); longer keywords match
    plainly because they cannot collide.
    """
    lowered = text.lower()
    detected: list[str] = []
    # (canonical code, list of (alias, requires_word_boundary)).
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


def _normalise_sala(text: str) -> str | None:
    match = _SALA_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(0)
    if "tercera" in raw.lower() or "3" in raw:
        return "Sala Tercera (Contencioso-Administrativo)"
    if "contencioso" in raw.lower():
        return "Sala de lo Contencioso-Administrativo"
    return raw.strip()


def _extract_first_date(text: str) -> date | None:
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _extract_after_label(text: str, label: str) -> str | None:
    pattern = re.compile(rf"{re.escape(label)}\s*:?\s*([^\n.]{{1,80}})", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().rstrip(".,;")
    # Trim trailing labels picked up by the loose regex.
    value = re.split(r"\s{2,}|\s—\s|\s-\s", value)[0]
    return value.strip() or None


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


class CENDOJCrawler(scrapy.Spider):
    """Downloads a curated list of CENDOJ resolution URLs.

    The list can come from a file (one URL per line) or be passed via the
    ``urls`` keyword (semicolon-separated). Each fetched page is stored as
    HTML plus a JSON sidecar with parsed metadata.
    """

    name = "CENDOJCrawler"
    custom_settings = {
        "DOWNLOAD_DELAY": float(os.environ.get("CENDOJ_DOWNLOAD_DELAY", "2.0")),
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "USER_AGENT": CENDOJ_DEFAULT_USER_AGENT,
    }

    def __init__(
        self,
        folder: str = "./data/cendoj",
        urls: str | None = None,
        urls_file: str | None = None,
        snapshot_date: str | None = None,
        only_tributario: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
        self.folder = os.path.join(folder, snapshot)
        self.only_tributario = _to_bool(only_tributario)
        os.makedirs(self.folder, exist_ok=True)
        self._urls = _collect_urls(urls=urls, urls_file=urls_file)

    def start_requests(self) -> Generator[scrapy.Request, None, None]:
        for index, url in enumerate(self._urls):
            yield scrapy.Request(url, callback=self.parse_document, cb_kwargs={"slot": index})

    async def start(self):  # type: ignore[override]
        for index, url in enumerate(self._urls):
            yield scrapy.Request(url, callback=self.parse_document, cb_kwargs={"slot": index})

    def parse_document(self, response: Response, slot: int) -> None:
        html = response.text
        metadata = parse_cendoj_document(html, source_url=response.url)
        if self.only_tributario and not metadata["is_tributario"]:
            self.logger.debug("Skipping non-tributario resolution at %s", response.url)
            return
        slug = metadata.get("roj") or metadata.get("ecli") or f"doc_{slot:05d}"
        slug = _slugify(slug)
        with open(os.path.join(self.folder, f"{slug}.html"), "w", encoding="utf-8") as fh:
            fh.write(html)
        with open(os.path.join(self.folder, f"{slug}.json"), "w", encoding="utf-8") as fj:
            json.dump(metadata, fj, ensure_ascii=False, indent=2)


def _collect_urls(*, urls: str | None, urls_file: str | None) -> list[str]:
    collected: list[str] = []
    if urls:
        collected.extend(part.strip() for part in urls.split(";") if part.strip())
    if urls_file:
        with open(urls_file, "r", encoding="utf-8") as fh:
            collected.extend(line.strip() for line in fh if line.strip() and not line.startswith("#"))
    return collected


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "doc"


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
