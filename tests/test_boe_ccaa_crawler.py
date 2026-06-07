from __future__ import annotations

import http.server
import json
import multiprocessing
from pathlib import Path
import socket
import threading

import pytest

from hacienda_gpt.crawler.boe_consolidado import (
    BOE_BASE_URL,
    CCAA_REGIMEN_COMUN,
    DEFAULT_CCAA_CODES,
    BOECCAACrawler,
    build_code_urls,
)


def test_regimen_comun_excludes_pais_vasco_and_navarra() -> None:
    foral_keys = {"pais_vasco", "navarra"}
    assert foral_keys.isdisjoint(CCAA_REGIMEN_COMUN.keys())
    assert len(CCAA_REGIMEN_COMUN) == 15


def test_default_codes_cover_all_regimen_comun() -> None:
    assert set(DEFAULT_CCAA_CODES.keys()) == set(CCAA_REGIMEN_COMUN.keys())


def test_build_code_urls_produces_catalog_and_pdf_links() -> None:
    urls = build_code_urls("andalucia")
    assert urls["catalog"] == f"{BOE_BASE_URL}/codigo.php?id=229&modo=2"
    assert urls["pdf"] == f"{BOE_BASE_URL}/abrir_pdf.php?fich=229_Codigo_de_Andalucia.pdf"


def test_build_code_urls_honours_custom_base_url() -> None:
    urls = build_code_urls("madrid", base_url="https://example.test/codigos")
    assert urls["catalog"].startswith("https://example.test/codigos/codigo.php")
    assert urls["pdf"].startswith("https://example.test/codigos/abrir_pdf.php")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _BOEHandler(http.server.BaseHTTPRequestHandler):
    """Minimal fixture server that returns either a fake PDF or a 404."""

    pdf_targets: set[str] = set()

    def do_GET(self) -> None:  # noqa: N802
        if "abrir_pdf.php" not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        # Extract the filename query param.
        fich = self.path.split("fich=")[1] if "fich=" in self.path else ""
        if fich in self.pdf_targets:
            body = b"%PDF-1.4\n%fake-consolidated-code\n%%EOF"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def boe_fixture_server() -> tuple[str, http.server.HTTPServer]:
    port = _free_port()
    handler = _BOEHandler
    handler.pdf_targets = {
        DEFAULT_CCAA_CODES["andalucia"]["filename"] + ".pdf",
        DEFAULT_CCAA_CODES["madrid"]["filename"] + ".pdf",
    }
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", server
    server.shutdown()
    thread.join(timeout=2)


def _run_crawl(crawler_cls, settings: dict, crawl_kwargs: dict) -> None:
    """Run a Scrapy crawl to completion. Executed in a forked subprocess: Scrapy
    starts the global Twisted reactor, which cannot restart within a process, so
    each crawl needs its own process to stay independent of test order."""
    from scrapy.crawler import CrawlerProcess

    process = CrawlerProcess(settings=settings)
    process.crawl(crawler_cls, **crawl_kwargs)
    process.start(install_signal_handlers=False)


def test_crawler_persists_only_pdfs_returned(
    tmp_path: Path,
    boe_fixture_server: tuple[str, http.server.HTTPServer],
) -> None:
    pytest.importorskip("scrapy")
    import scrapy as _scrapy

    if not hasattr(_scrapy, "__path__"):
        pytest.skip("scrapy stubbed by conftest; integration test needs the real package")

    base_url, _server = boe_fixture_server
    snapshot = "2026-05-28"
    settings = {
        "TELNETCONSOLE_ENABLED": False,
        "LOG_ENABLED": False,
        "DOWNLOAD_DELAY": 0,
        "ROBOTSTXT_OBEY": False,
    }
    crawl_kwargs = {
        "folder": str(tmp_path),
        "ccaa": "andalucia,madrid,galicia",  # galicia is mapped but server serves only AN+MD
        "snapshot_date": snapshot,
        "base_url": base_url,
    }
    proc = multiprocessing.get_context("fork").Process(target=_run_crawl, args=(BOECCAACrawler, settings, crawl_kwargs))
    proc.start()
    proc.join(120)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise AssertionError("crawl subprocess timed out")
    assert proc.exitcode == 0, "crawl subprocess failed"

    snap_dir = tmp_path / snapshot / "boe_ccaa"
    andalucia_meta = snap_dir / "andalucia" / "metadata.json"
    madrid_meta = snap_dir / "madrid" / "metadata.json"
    galicia_meta = snap_dir / "galicia" / "metadata.json"

    assert andalucia_meta.exists()
    assert madrid_meta.exists()
    assert not galicia_meta.exists(), "Galicia should be skipped (server returned 404)"

    andalucia = json.loads(andalucia_meta.read_text(encoding="utf-8"))
    assert andalucia["source_authority"] == "BOE"
    assert andalucia["jurisdiction_scope"] == "autonomico:AN"
    assert andalucia["covered_tributos"] == ["IRPF_autonomico", "ISD", "ITP_AJD"]
    assert andalucia["pdf_size_bytes"] > 0
