from collections.abc import Generator
from datetime import UTC, datetime
import hashlib
import os
import re
from typing import Any
from urllib.parse import urlparse

from pathvalidate import sanitize_filepath
import scrapy
from scrapy.http import Response
from scrapy.linkextractors import LinkExtractor
from scrapy_playwright.page import PageMethod


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
    ]
    allowed_paths = [
        r"/Sede/irpf",
        r"/Sede/iva",
        r"/Sede/censos-nif-domicilio-fiscal",
        r"/Sede/colaborar-agencia-tributaria/modelos",
        r"/Sede/recaudacion/aplazamientos-fraccionamientos-deudas-tributarias",
        r"/Sede/colaborar-agencia-tributaria/calendario-contribuyente",
        r"/Sede/ayuda/manuales-videos-folletos",
        r"/Sede/informacion-administrativa/sanciones",
    ]

    def __init__(self, folder: str = "./data/html", mode: str = "flat", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        snapshot_date = kwargs.get("snapshot_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        self.folder = os.path.join(folder, snapshot_date)
        self.mode = mode
        self.allowed_patterns: list[re.Pattern] = [re.compile(path) for path in self.allowed_paths]
        self._ensure_folder_exists()

    def start_requests(self) -> Generator[scrapy.Request, None, None]:
        for url in self.urls:
            yield scrapy.Request(url, meta=self._get_meta_for_request(url), callback=self.parse)

    def parse(self, response: Response) -> Generator[scrapy.Request, None, None]:
        self._save_response_to_file(response)
        yield from self._follow_domain_links(response)

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

    def _follow_domain_links(self, response: Response) -> Generator[scrapy.Request, None, None]:
        link_extractor = LinkExtractor(allow=self.allowed_patterns, allow_domains=["sede.agenciatributaria.gob.es"])
        for link in link_extractor.extract_links(response):
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

    def parse(self, response: Response) -> Generator[dict[str, list[str] | str] | scrapy.Request, None, None]:
        self.seen_urls.add(response.url)
        extractor = LinkExtractor(allow_domains=[self._extract_domain_from_start_url()], unique=True)
        for link in extractor.extract_links(response):
            if link.url in self.seen_urls:
                continue
            if self._is_pdf_url(link.url):
                yield scrapy.Request(link.url, callback=self.parse_pdf)
            else:
                self.seen_urls.add(link.url)
                yield scrapy.Request(link.url, callback=self.parse)

    def parse_pdf(self, response: Response) -> Generator[dict[str, list[str] | str], None, None]:
        if not self._is_pdf_response(response):
            return
        content_hash = hashlib.sha256(response.body).hexdigest()
        if content_hash in self.seen_content_hashes:
            return
        self.seen_content_hashes.add(content_hash)
        yield {"file_urls": [response.url], "path": self.folder}

    def _is_pdf_url(self, url: str) -> bool:
        return url.lower().endswith(".pdf")

    def _is_pdf_response(self, response: Response) -> bool:
        content_type = response.headers.get("Content-Type", b"").decode("utf-8", errors="ignore").lower()
        return "application/pdf" in content_type or self._is_pdf_url(response.url)

    def _extract_domain_from_start_url(self) -> str:
        return urlparse(self.start_urls[0]).netloc
