"""Crawler for the DYCTEA database of TEAC and TEARs resolutions.

DYCTEA is an ASP.NET WebForms application served at:
    https://serviciostelematicosext.hacienda.gob.es/TEAC/DYCTEA/

The detail page for a single resolution criterion is reached via
`criterio.aspx?id=<internal_id>`. Non-existent ids respond with the literal
text "El criterio no existe.".

Two ingestion strategies are exposed:

- **id-sweep**: iterates an integer range and keeps the criteria that exist.
  Robust against UI changes, suitable for the initial bulk index.
- **search-form**: drives the search form on the landing page (postback
  with ``__VIEWSTATE`` / ``__EVENTVALIDATION``) and follows result links.
  Suitable for incremental updates filtered by date or by *Unidad*.

For each criterion the crawler stores both the raw HTML and a JSON sidecar
with the parsed metadata, so the downstream processor can index either
representation without re-fetching.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Generator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import parsel
import scrapy
from scrapy.http import Response

DYCTEA_BASE_URL = "https://serviciostelematicosext.hacienda.gob.es/TEAC/DYCTEA"
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


def parse_teac_criterio(html: str, *, criterio_id: int | None = None) -> dict[str, Any] | None:
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
        value = selector.xpath(
            f"//th[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}']/following-sibling::td[1]//text()"
        ).getall() or selector.xpath(
            f"//td[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}']/following-sibling::td[1]//text()"
        ).getall() or selector.xpath(
            f"//td[normalize-space(translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú'))='{alias}:']/following-sibling::td[1]//text()"
        ).getall()
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


def _normalise_sentido(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if "desestimat" in lowered:
        return "desestimatoria"
    if "estimat" in lowered and "parcial" in lowered:
        return "estimatoria_parcial"
    if "estimat" in lowered:
        return "estimatoria"
    if "inadmis" in lowered:
        return "inadmision"
    return value.strip().lower()


def _normalise_vinculante(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.lower().strip()
    if "sí" in lowered or "si" == lowered or lowered.startswith("vinculante"):
        return True
    if "no" in lowered:
        return False
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


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


class TEACCrawler(scrapy.Spider):
    """DYCTEA id-sweep crawler.

    For each id in ``[start_id, end_id]`` it fetches ``criterio.aspx?id=N`` and
    saves the HTML + JSON sidecar when the page is not the "no existe" stub.
    The id range and base URL are configurable so the spider can be smoke
    tested against a local fixture server.
    """

    name = "TEACCrawler"
    custom_settings = {
        "DOWNLOAD_DELAY": float(os.environ.get("TEAC_DOWNLOAD_DELAY", "1.5")),
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "USER_AGENT": "hacienda-asesor/0.1 (+contacto@hacienda-asesor.local)",
    }

    def __init__(
        self,
        folder: str = "./data/teac",
        start_id: int = 1,
        end_id: int = 100,
        snapshot_date: str | None = None,
        base_url: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
        self.folder = os.path.join(folder, snapshot)
        self.start_id = int(start_id)
        self.end_id = int(end_id)
        self.base_url = (base_url or DYCTEA_BASE_URL).rstrip("/")
        os.makedirs(self.folder, exist_ok=True)

    def start_requests(self) -> Generator[scrapy.Request, None, None]:
        # Legacy entry point for scrapy < 2.13.
        for criterio_id in range(self.start_id, self.end_id + 1):
            url = f"{self.base_url}/criterio.aspx?{urlencode({'id': criterio_id})}"
            yield scrapy.Request(url, callback=self.parse_criterio, cb_kwargs={"criterio_id": criterio_id})

    async def start(self):  # type: ignore[override]
        # Required entry point for scrapy >= 2.13.
        for criterio_id in range(self.start_id, self.end_id + 1):
            url = f"{self.base_url}/criterio.aspx?{urlencode({'id': criterio_id})}"
            yield scrapy.Request(url, callback=self.parse_criterio, cb_kwargs={"criterio_id": criterio_id})

    def parse_criterio(self, response: Response, criterio_id: int) -> None:
        html = response.text
        metadata = parse_teac_criterio(html, criterio_id=criterio_id)
        if metadata is None:
            self.logger.debug("Criterio %s no existe; skipping.", criterio_id)
            return
        self._persist(criterio_id, html, metadata)

    def _persist(self, criterio_id: int, html: str, metadata: dict[str, Any]) -> None:
        html_path = os.path.join(self.folder, f"criterio_{criterio_id}.html")
        json_path = os.path.join(self.folder, f"criterio_{criterio_id}.json")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        with open(json_path, "w", encoding="utf-8") as fj:
            json.dump(metadata, fj, ensure_ascii=False, indent=2)
