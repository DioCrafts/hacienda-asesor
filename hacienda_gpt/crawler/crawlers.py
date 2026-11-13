from collections.abc import Generator
from datetime import UTC, datetime
import hashlib
import os
import re
from typing import Any
from urllib.parse import urlparse

from pathvalidate import sanitize_filepath
from pydantic import BaseModel, ConfigDict, Field, field_validator
import scrapy
from scrapy.http import HtmlResponse, Response
from scrapy.linkextractors import LinkExtractor
from scrapy.spidermiddlewares.httperror import HttpError
from scrapy_playwright.page import PageMethod

from hacienda_gpt.crawler.boe_consolidado import BOECCAACrawler  # re-export
from hacienda_gpt.crawler.boletin_autonomico import RegionalBoletinCrawler  # re-export
from hacienda_gpt.crawler.cendoj import CENDOJCrawler  # re-export
from hacienda_gpt.crawler.teac import TEACCrawler  # re-export
from hacienda_gpt.ops import formatos

__all__ = [
    "AgenciaTributariaWebCrawler",
    "AgenciaTributariaPDFCrawler",
    "BOECCAACrawler",
    "RegionalBoletinCrawler",
    "CENDOJCrawler",
    "TEACCrawler",
]


SEDE_AEAT = "https://sede.agenciatributaria.gob.es/"
_BASE_HREF_RE = re.compile(r"""<base\s[^>]*href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class ReglasSedeAeat(BaseModel):
    """Which links of the AEAT sede the web crawler follows, as format-spec data (``aeat_sede``).

    Version 1 holds ``AgenciaTributariaWebCrawler.allowed_paths`` and its
    domain; a reviewed proposal may add the paths of a reorganised sede.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Regular expressions searched in each link's URL (Scrapy ``LinkExtractor(allow=…)``).
    rutas_permitidas: tuple[str, ...] = Field(min_length=1)
    dominios: tuple[str, ...] = Field(min_length=1)

    @field_validator("rutas_permitidas")
    @classmethod
    def _patrones(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(formatos.patron_seguro(patron) for patron in value)

    @field_validator("dominios")
    @classmethod
    def _dominios(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[a-z0-9.-]+\.gob\.es", dominio) for dominio in value):
            raise ValueError("solo se admiten dominios de la Administración (*.gob.es)")
        return value


def extraer_enlaces(reglas: ReglasSedeAeat, html: str, url: str | None = None) -> list[str]:
    """The links of one sede page the crawler follows under ``reglas``, in page order.

    ``url`` is the page's address; without it, a ``<base href>`` in ``html``
    (the format watch keeps one in its samples) or the sede's root is used.
    """
    if url is None:
        base = _BASE_HREF_RE.search(html[:4096])
        url = base.group(1) if base else SEDE_AEAT
    response = HtmlResponse(url=url, body=html.encode("utf-8"), encoding="utf-8")
    extractor = LinkExtractor(
        allow=[re.compile(p) for p in reglas.rutas_permitidas], allow_domains=list(reglas.dominios)
    )
    return [link.url for link in extractor.extract_links(response)]


class AgenciaTributariaWebCrawler(scrapy.Spider):
    name = "AgenciaTributariaWebCrawler"
    urls = [
        "https://sede.agenciatributaria.gob.es/",
        "https://sede.agenciatributaria.gob.es/Sede/irpf.html",
        "https://sede.agenciatributaria.gob.es/Sede/iva.html",
        "https://sede.agenciatributaria.gob.es/Sede/censos-nif-domicilio-fiscal.html",
        "https://sede.agenciatributaria.gob.es/Sede/colaborar-agencia-tributaria/modelos-100-199.html",
        "https://sede.agenciatributaria.gob.es/Sede/recaudacion/aplazamientos-fraccionamientos-deudas-tributarias.html",
        "https://sede.agenciatributaria.gob.es/Sede/colaborar-agencia-tributaria/calendario-contribuyente.html",
        "https://sede.agenciatributaria.gob.es/Sede/ayuda/manuales-videos-folletos/manuales-practicos.html",
        "https://sede.agenciatributaria.gob.es/Sede/no-residentes.html",
        "https://sede.agenciatributaria.gob.es/Sede/declaraciones-informativas.html",
        "https://sede.agenciatributaria.gob.es/Sede/declaraciones-informativas-otros-impuestos-tasas.html",
    ]
    allowed_paths = [
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

    def __init__(self, folder: str = "./data/html", mode: str = "flat", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        snapshot_date = kwargs.get("snapshot_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        self.folder = os.path.join(folder, snapshot_date)
        self.mode = mode
        # The paths to follow are the ``aeat_sede`` format spec (version 1: ``allowed_paths`` above).
        self.reglas = _reglas_sede(self.allowed_paths)
        self.allowed_patterns: list[re.Pattern] = [re.compile(path) for path in self.reglas.rutas_permitidas]
        self._ensure_folder_exists()
        # Format watch: what this run read (reported once, when the spider closes).
        self._semillas = set(self.urls)
        self._paginas = 0
        self._semillas_fallidas: list[dict[str, Any]] = []

    def start_requests(self) -> Generator[scrapy.Request]:
        for url in self.urls:
            yield scrapy.Request(
                url, meta=self._get_meta_for_request(url), callback=self.parse, errback=self._semilla_fallida
            )

    def parse(self, response: Response) -> Generator[scrapy.Request]:
        self._save_response_to_file(response)
        self._paginas += 1
        if response.url in self._semillas:
            # Format watch (never raises): a reorganised sede still answers, with no link the crawler follows.
            # The ``<base>`` keeps the page's address in the sample, so the links can be replayed from it.
            formatos.observar(
                "aeat_sede", f'<base href="{response.url}">' + getattr(response, "text", ""), contexto=response.url
            )
        yield from self._follow_domain_links(response)

    def _semilla_fallida(self, failure: Any) -> None:
        request = getattr(failure, "request", None)
        status = failure.value.response.status if failure.check(HttpError) else None
        self._semillas_fallidas.append({"url": request.url if request else "?", "http": status})
        self.logger.warning("AEAT seed page failed: %s (%s)", request.url if request else "?", failure.value)

    def closed(self, reason: str) -> None:
        if not self._paginas and not any(fallo["http"] for fallo in self._semillas_fallidas):
            # Nothing answered at all (no network): that says nothing about the sede's format.
            return
        formatos.observar(
            "aeat_rastreo",
            None,
            extraccion={
                "paginas": self._paginas,
                "semillas": len(self._semillas),
                "semillas_fallidas": self._semillas_fallidas,
            },
            contexto=reason,
        )

    def _get_meta_for_request(self, url: str) -> dict[str, Any]:
        return {"playwright": True, "playwright_page_methods": self._get_page_methods(url)}

    def _get_page_methods(self, url: str) -> list[PageMethod]:
        return [
            PageMethod("wait_for_load_state", "networkidle"),
            PageMethod("screenshot", path=self._generate_screenshot_path(url), full_page=True),
        ]

    def _ensure_folder_exists(self) -> None:
        os.makedirs(self.folder, exist_ok=True)

    def _save_response_to_file(self, response: Response) -> None:
        with open(self._generate_file_path(response), "wb") as file_pointer:
            file_pointer.write(response.body)

    def _follow_domain_links(self, response: Response) -> Generator[scrapy.Request]:
        link_extractor = LinkExtractor(allow=self.allowed_patterns, allow_domains=list(self.reglas.dominios))
        # Scrapy types callbacks with the base Response; pages here are HTML (TextResponse).
        for link in link_extractor.extract_links(response):  # type: ignore[arg-type]
            yield scrapy.Request(url=link.url, meta=self._get_meta_for_request(link.url), callback=self.parse)

    def _generate_screenshot_path(self, url: str) -> str:
        return os.path.join(self.folder, f"{hashlib.md5(url.encode()).hexdigest()}.png")

    def _generate_file_path(self, response: Response) -> str:
        return (
            os.path.join(self.folder, f"{hashlib.md5(response.url.encode()).hexdigest()}.html")
            if self.mode == "flat"
            else self._generate_structure_file_path(response)
        )

    def _generate_structure_file_path(self, response: Response) -> str:
        parsed_url = urlparse(response.url)
        full_path = os.path.join(self.folder, sanitize_filepath(parsed_url.path[1:]))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        return full_path


class AgenciaTributariaPDFCrawler(scrapy.Spider):
    name = "AgenciaTributariaPDFCrawler"
    start_urls = ["https://sede.agenciatributaria.gob.es/"]

    def __init__(self, folder: str | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        snapshot_date = kwargs.get("snapshot_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        base_folder = folder or "./data/pdf"
        self.folder = os.path.join(base_folder, snapshot_date)
        self.seen_urls: set[str] = set()
        self.seen_content_hashes: set[str] = set()
        os.makedirs(self.folder, exist_ok=True)
        # Format watch: PDF answers, how many were not a PDF (a rendered viewer) and how many were saved.
        self._recuento = {"respuestas_pdf": 0, "no_pdf": 0, "guardados": 0, "duplicados": 0}

    def parse(self, response: Response) -> Generator[scrapy.Request]:
        self.seen_urls.add(response.url)
        extractor = LinkExtractor(allow_domains=[self._extract_domain_from_start_url()], unique=True)
        # Scrapy types callbacks with the base Response; pages here are HTML (TextResponse).
        for link in extractor.extract_links(response):  # type: ignore[arg-type]
            if link.url in self.seen_urls:
                continue
            self.seen_urls.add(link.url)
            callback = self.parse_pdf if self._is_pdf_url(link.url) else self.parse
            yield scrapy.Request(link.url, callback=callback)

    def parse_pdf(self, response: Response) -> None:
        # Persist the bytes Scrapy already fetched. We deliberately do NOT use
        # Scrapy's FilesPipeline: every HTTPS request in this project is forced
        # through scrapy-playwright (the stock TLS downloader is broken on the
        # pinned stack), so a FilesPipeline-issued request would re-render the
        # PDF in Chromium instead of returning raw bytes. Writing response.body
        # here mirrors how AgenciaTributariaWebCrawler saves HTML.
        if not self._is_pdf_response(response):
            return
        self._recuento["respuestas_pdf"] += 1
        body = response.body
        if not body or not body.startswith(b"%PDF"):
            self._recuento["no_pdf"] += 1
            self.logger.warning(
                "Skipping %s: response body is not a PDF (%d bytes); playwright may have rendered it.",
                response.url,
                len(body or b""),
            )
            return
        content_hash = hashlib.sha256(body).hexdigest()
        if content_hash in self.seen_content_hashes:
            self._recuento["duplicados"] += 1
            return
        self.seen_content_hashes.add(content_hash)
        path = self._generate_pdf_path(response.url)
        with open(path, "wb") as file_pointer:
            file_pointer.write(body)
        self._recuento["guardados"] += 1
        self.logger.info("Saved PDF %s -> %s", response.url, path)

    def closed(self, reason: str) -> None:
        if not self.seen_urls and not self._recuento["respuestas_pdf"]:
            # Nothing was read (no network): that says nothing about the sede's format.
            return
        formatos.observar(
            "aeat_pdf", None, extraccion={**self._recuento, "paginas": len(self.seen_urls)}, contexto=reason
        )

    def _generate_pdf_path(self, url: str) -> str:
        # basename strips any directory component, so path traversal via the URL
        # is impossible; sanitize_filepath then removes illegal filename chars.
        name = sanitize_filepath(os.path.basename(urlparse(url).path))
        if not name.lower().endswith(".pdf"):
            name = f"{hashlib.md5(url.encode()).hexdigest()}.pdf"
        return os.path.join(self.folder, name)

    def _is_pdf_url(self, url: str) -> bool:
        return url.lower().endswith(".pdf")

    def _is_pdf_response(self, response: Response) -> bool:
        content_type = (response.headers.get("Content-Type") or b"").decode("utf-8", errors="ignore").lower()
        return "application/pdf" in content_type or self._is_pdf_url(response.url)

    def _extract_domain_from_start_url(self) -> str:
        return urlparse(self.start_urls[0]).netloc


def _reglas_sede(rutas_de_siempre: list[str]) -> ReglasSedeAeat:
    """The ``aeat_sede`` rules in force; the crawler's own paths if the spec cannot be read."""
    try:
        return formatos.reglas_de("aeat_sede", ReglasSedeAeat)
    except formatos.FormatoError:
        return ReglasSedeAeat(rutas_permitidas=tuple(rutas_de_siempre), dominios=("sede.agenciatributaria.gob.es",))
