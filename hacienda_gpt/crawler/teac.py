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

from collections.abc import Generator
from datetime import UTC, date, datetime
import json
import os
import re
from typing import Any
from urllib.parse import urlencode

import parsel
from pydantic import BaseModel, ConfigDict, field_validator
import scrapy
from scrapy.http import Response

from hacienda_gpt.ops import formatos

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

# Keys of the sidecar that are not read from the page.
_NO_EXTRAIDO = frozenset({"source_authority", "criterio_id", "ingested_at"})

_NUM_RESOLUCION_PATTERN = re.compile(r"\b\d{2}/\d{4,6}/\d{4}(?:/\d{2}/\d{2})?\b")
_DATE_PATTERN = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

# How a labelled value is looked up, as XPath templates where ``{alias}`` is a lowercase label. Groups are
# tried in order; inside a group the first template that matches anything wins, and the group's value is its
# non-blank text joined by spaces (an all-blank value moves on to the next group). Version 1 of the spec.
_MINUSCULAS = "translate(text(), 'ABCDEFGHIJKLMNÑOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnñopqrstuvwxyzáéíóú')"
PLANTILLAS_V1: tuple[tuple[str, ...], ...] = (
    # dt/dd pattern (definition list)
    (f"//dt[normalize-space({_MINUSCULAS})='{{alias}}']/following-sibling::dd[1]//text()",),
    # tr/td pattern
    (
        f"//th[normalize-space({_MINUSCULAS})='{{alias}}']/following-sibling::td[1]//text()",
        f"//td[normalize-space({_MINUSCULAS})='{{alias}}']/following-sibling::td[1]//text()",
        f"//td[normalize-space({_MINUSCULAS})='{{alias}}:']/following-sibling::td[1]//text()",
    ),
    # label + span/strong pattern
    (f"//*[normalize-space({_MINUSCULAS})='{{alias}}:']/following-sibling::*[1]//text()",),
)


class ReglasTeac(BaseModel):
    """What the parser knows about the DYCTEA page, as format-spec data (``teac_criterio``).

    Version 1 of the spec holds ``DYCTEA_NOT_FOUND_MARKER``, ``_FIELD_ALIASES``,
    the two patterns and the XPath templates above; a reviewed proposal may
    replace them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    marcador_no_existe: str
    # Canonical field → labels it may appear under (lower case).
    alias: dict[str, tuple[str, ...]]
    patron_numero: str
    patron_fecha: str
    # Groups of XPath templates with an ``{alias}`` placeholder (see ``PLANTILLAS_V1``).
    plantillas: tuple[tuple[str, ...], ...] = PLANTILLAS_V1

    @field_validator("plantillas")
    @classmethod
    def _plantillas(cls, value: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
        if not value or any(not grupo for grupo in value):
            raise ValueError("hace falta al menos un grupo de plantillas, y ningún grupo vacío")
        prueba = parsel.Selector(text="<html><body></body></html>")
        for plantilla in (p for grupo in value for p in grupo):
            if plantilla.count("{alias}") != 1 or len(plantilla) > formatos.MAX_PATRON:
                raise ValueError("cada plantilla lleva «{alias}» exactamente una vez y es breve")
            try:
                prueba.xpath(plantilla.replace("{alias}", "x"))
            except ValueError as exc:
                raise ValueError(f"XPath no válido: {exc}") from exc
        return value

    @field_validator("patron_numero", "patron_fecha")
    @classmethod
    def _patron(cls, value: str) -> str:
        return formatos.patron_seguro(value)

    @field_validator("patron_fecha")
    @classmethod
    def _tres_grupos(cls, value: str) -> str:
        if re.compile(value).groups != 3:
            raise ValueError("el patrón de fecha debe tener tres grupos: día, mes y año")
        return value

    @field_validator("alias")
    @classmethod
    def _campos(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        if set(value) != set(_FIELD_ALIASES):
            raise ValueError(f"los campos deben ser exactamente: {', '.join(sorted(_FIELD_ALIASES))}")
        if any("'" in alias or not alias.strip() for aliases in value.values() for alias in aliases):
            raise ValueError("una etiqueta no puede estar vacía ni contener comillas simples")
        return value


def extraer_criterio(reglas: ReglasTeac, html: str) -> dict[str, Any] | None:
    """The fields of a DYCTEA page according to ``reglas``; None for the "no existe" placeholder."""
    if reglas.marcador_no_existe in html:
        return None

    selector = parsel.Selector(text=html)
    fields: dict[str, str | None] = {}
    for canonical, aliases in reglas.alias.items():
        fields[canonical] = _extract_field(selector, aliases, reglas.plantillas)

    numero = fields.get("numero_resolucion") or _first_match(re.compile(reglas.patron_numero), html)
    fecha = _parse_date(fields.get("fecha_resolucion") or "", re.compile(reglas.patron_fecha))

    return {
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
    }


def parse_teac_criterio(html: str, *, criterio_id: int | str | None = None) -> dict[str, Any] | None:
    """Parse a DYCTEA resolution detail page into a structured dict.

    Returns None when the page is the "criterio no existe" error placeholder,
    so callers can short-circuit storage.
    """
    campos = extraer_criterio(formatos.reglas_de("teac_criterio", ReglasTeac), html)
    if campos is None:
        return None
    numero = campos.pop("numero_resolucion")
    return {
        "source_authority": "TEAC",
        "criterio_id": criterio_id,
        "numero_resolucion": numero,
        **campos,
        "ingested_at": datetime.now(UTC).isoformat(),
    }


def observar_criterio(html: str, metadata: dict[str, Any], criterio_id: int | str) -> None:
    """Format watch (never raises): a page whose labels changed still parses, with every field empty."""
    formatos.observar(
        "teac_criterio",
        html,
        extraccion={k: v for k, v in metadata.items() if k not in _NO_EXTRAIDO},
        contexto=str(criterio_id),
    )


def _extract_field(
    selector: Any, aliases: tuple[str, ...], plantillas: tuple[tuple[str, ...], ...] = PLANTILLAS_V1
) -> str | None:
    """Look up a labelled value tolerating dl/dt/dd, table tr/td and label/span layouts (``plantillas``)."""
    for alias in aliases:
        for grupo in plantillas:
            value: list[str] = []
            for plantilla in grupo:
                value = selector.xpath(plantilla.replace("{alias}", alias)).getall()
                if value:
                    break
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
    # Negation must win over the positive "vinculante" prefix: "No vinculante",
    # "Vinculante: No", etc. are NOT binding. Check it FIRST — otherwise
    # ``startswith("vinculante")`` would shadow the "no" and wrongly return True.
    # The word boundary (\bno\b) avoids tripping on "no" embedded in other words.
    if re.search(r"\bno\b", lowered):
        return False
    if re.search(r"\bs[ií]\b", lowered) or lowered.startswith("vinculante"):
        return True
    return None


def _parse_date(value: str, pattern: re.Pattern[str] = _DATE_PATTERN) -> date | None:
    match = pattern.search(value)
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
        folder: str = "./data/html",
        start_id: int = 1,
        end_id: int = 100,
        snapshot_date: str | None = None,
        base_url: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
        # Land under the processor's content_dir, namespaced by source:
        # ./data/html/<snapshot>/teac/
        self.folder = os.path.join(folder, snapshot, "teac")
        self.start_id = int(start_id)
        self.end_id = int(end_id)
        self.base_url = (base_url or DYCTEA_BASE_URL).rstrip("/")
        os.makedirs(self.folder, exist_ok=True)

    def start_requests(self) -> Generator[scrapy.Request]:
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
        observar_criterio(html, metadata, criterio_id)
        self._persist(criterio_id, html, metadata)

    def _persist(self, criterio_id: int, html: str, metadata: dict[str, Any]) -> None:
        html_path = os.path.join(self.folder, f"criterio_{criterio_id}.html")
        json_path = os.path.join(self.folder, f"criterio_{criterio_id}.json")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        with open(json_path, "w", encoding="utf-8") as fj:
            json.dump(metadata, fj, ensure_ascii=False, indent=2)
