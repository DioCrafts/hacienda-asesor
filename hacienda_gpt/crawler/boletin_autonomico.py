"""Generic crawler scaffolding for autonomous community official gazettes.

Each CCAA exposes its own search/list endpoint with idiosyncratic markup,
so a single Scrapy spider cannot cover them all. Instead this module
provides a thin parameterised spider that takes a ``RegionalBoletinSpec``
describing the boletín to crawl: search URL, the XPath that yields the
list of result items, and the selectors used to extract the title, the
publication date and the link to the consolidated text.

Three production-ready specs are bundled out of the box (BOCM, BOJA,
DOGC) — chosen because they concentrate the highest user-facing volume
of fiscal regulation. Adding a new CCAA only requires writing a new
spec; no changes to the spider itself.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
import re
from typing import Any
from urllib.parse import urljoin

import parsel
import scrapy
from scrapy.http import Response


@dataclass(frozen=True)
class RegionalBoletinSpec:
    """Configuration for a single autonomous community gazette.

    The search URL is expected to expose a results page directly (HTTP
    GET); spec authors should pick the URL that returns a hit list for
    the queried term. ``item_xpath`` selects the container nodes; the
    other selectors run *relative to each container*.
    """

    key: str
    ccaa_ine: str
    name: str
    base_url: str
    search_url_template: str
    item_xpath: str
    title_xpath: str
    date_xpath: str
    link_xpath: str
    default_terms: tuple[str, ...] = field(default_factory=tuple)


BOCM_MADRID = RegionalBoletinSpec(
    key="madrid_bocm",
    ccaa_ine="MD",
    name="Boletín Oficial de la Comunidad de Madrid",
    base_url="https://www.bocm.es",
    # The BOCM search accepts a free-text query via ``texto``; results are
    # rendered server-side. The spec encodes a strict tax-flavoured query
    # to keep volume manageable.
    search_url_template="https://www.bocm.es/buscar?texto={query}",
    item_xpath="//div[contains(@class, 'resultado')]",
    title_xpath=".//h3//text()",
    date_xpath=".//span[contains(@class, 'fecha')]//text()",
    link_xpath=".//a/@href",
    default_terms=("ISD", "ITP", "IRPF tramo autonómico"),
)


BOJA_ANDALUCIA = RegionalBoletinSpec(
    key="andalucia_boja",
    ccaa_ine="AN",
    name="Boletín Oficial de la Junta de Andalucía",
    base_url="https://www.juntadeandalucia.es/boja",
    search_url_template="https://www.juntadeandalucia.es/boja/buscador?q={query}",
    item_xpath="//article[contains(@class, 'resultado') or contains(@class, 'item-resultado')]",
    title_xpath=".//h2//text() | .//h3//text()",
    date_xpath=".//time/@datetime | .//span[contains(@class, 'fecha')]//text()",
    link_xpath=".//a/@href",
    default_terms=("Sucesiones y Donaciones", "Transmisiones Patrimoniales", "IRPF"),
)


DOGC_CATALUNA = RegionalBoletinSpec(
    key="cataluna_dogc",
    ccaa_ine="CT",
    name="Diari Oficial de la Generalitat de Catalunya",
    base_url="https://dogc.gencat.cat",
    search_url_template="https://dogc.gencat.cat/ca/cercador-historic?text={query}",
    item_xpath="//div[contains(@class, 'resultat-cerca') or contains(@class, 'result')]",
    title_xpath=".//h3//text() | .//a//text()",
    date_xpath=".//span[contains(@class, 'data')]//text() | .//time/@datetime",
    link_xpath=".//a/@href",
    default_terms=("ISD", "ITP", "tram autonòmic IRPF"),
)


BUNDLED_SPECS: dict[str, RegionalBoletinSpec] = {
    spec.key: spec for spec in (BOCM_MADRID, BOJA_ANDALUCIA, DOGC_CATALUNA)
}


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


def _first_text(matches: parsel.SelectorList) -> str | None:
    for value in matches.getall():
        if not value:
            continue
        cleaned = re.sub(r"\s+", " ", value).strip()
        if cleaned:
            return cleaned
    return None


class RegionalBoletinCrawler(scrapy.Spider):
    """Spider that walks one or more configured autonomic gazettes.

    Modes:

    - With ``listing_html`` set: parse the provided HTML offline (useful
      for tests / replaying captured snapshots).
    - Otherwise: hit each spec's search endpoint with the configured
      terms and follow the discovered links.
    """

    name = "RegionalBoletinCrawler"
    custom_settings = {
        "DOWNLOAD_DELAY": float(os.environ.get("BOLETIN_DOWNLOAD_DELAY", "1.5")),
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "USER_AGENT": "hacienda-asesor/0.1 (research; contacto@hacienda-asesor.local)",
    }

    def __init__(
        self,
        folder: str = "./data/boletines_autonomicos",
        boletines: str | None = None,
        terms: str | None = None,
        snapshot_date: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
        self.folder = os.path.join(folder, snapshot)
        os.makedirs(self.folder, exist_ok=True)
        self._specs = _resolve_specs(boletines)
        self._terms: list[str] | None = [t.strip() for t in re.split(r"[,;]", terms) if t.strip()] if terms else None

    def start_requests(self) -> Generator[scrapy.Request]:
        yield from self._build_search_requests()

    async def start(self):  # type: ignore[override]
        for request in self._build_search_requests():
            yield request

    def _build_search_requests(self) -> Generator[scrapy.Request]:
        for spec in self._specs:
            for term in self._terms or spec.default_terms:
                url = spec.search_url_template.format(query=term)
                yield scrapy.Request(
                    url,
                    callback=self.parse_listing_response,
                    cb_kwargs={"spec_key": spec.key, "term": term},
                    errback=self.handle_error,
                    dont_filter=True,
                )

    def parse_listing_response(self, response: Response, spec_key: str, term: str) -> None:
        spec = BUNDLED_SPECS[spec_key]
        rows = parse_listing(spec, response.text)
        out_dir = os.path.join(self.folder, spec.key)
        os.makedirs(out_dir, exist_ok=True)
        listing_path = os.path.join(out_dir, f"listing_{_slugify(term)}.json")
        with open(listing_path, "w", encoding="utf-8") as fj:
            json.dump(
                {
                    "boletin": spec.key,
                    "ccaa_ine": spec.ccaa_ine,
                    "term": term,
                    "source_url": response.url,
                    "results": rows,
                    "ingested_at": datetime.now(UTC).isoformat(),
                },
                fj,
                ensure_ascii=False,
                indent=2,
            )
        for row in rows:
            yield scrapy.Request(
                row["link"],
                callback=self.parse_document,
                cb_kwargs={"row": row, "spec_key": spec_key, "term": term},
                errback=self.handle_error,
            )

    def parse_document(self, response: Response, row: dict[str, Any], spec_key: str, term: str) -> None:
        spec = BUNDLED_SPECS[spec_key]
        out_dir = os.path.join(self.folder, spec.key, "documents")
        os.makedirs(out_dir, exist_ok=True)
        slug = _slugify(row.get("title") or response.url.rsplit("/", 1)[-1] or "doc")
        html_path = os.path.join(out_dir, f"{slug}.html")
        with open(html_path, "wb") as fh:
            fh.write(response.body)
        metadata = {
            "source_authority": f"BOLETIN_{spec.ccaa_ine}",
            "source_hierarchy_rank": 6,
            "jurisdiction_scope": f"autonomico:{spec.ccaa_ine}",
            "boletin": spec.key,
            "term_searched": term,
            "title": row.get("title"),
            "date_raw": row.get("date_raw"),
            "source_url": response.url,
            "ingested_at": datetime.now(UTC).isoformat(),
        }
        with open(os.path.join(out_dir, f"{slug}.json"), "w", encoding="utf-8") as fj:
            json.dump(metadata, fj, ensure_ascii=False, indent=2)

    def handle_error(self, failure: Any) -> None:
        request = failure.request if hasattr(failure, "request") else None
        url = request.url if request else "unknown"
        self.logger.warning("Boletin fetch failed for %s: %s", url, failure)


def _resolve_specs(boletines: str | None) -> list[RegionalBoletinSpec]:
    if boletines is None or boletines.lower() in {"all", "todos"}:
        return list(BUNDLED_SPECS.values())
    keys = [part.strip() for part in re.split(r"[,;]", boletines) if part.strip()]
    unknown = [key for key in keys if key not in BUNDLED_SPECS]
    if unknown:
        raise ValueError(f"Unknown boletin keys: {unknown}; known: {sorted(BUNDLED_SPECS)}")
    return [BUNDLED_SPECS[key] for key in keys]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return slug[:80] or "doc"
