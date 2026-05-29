from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socket
import threading

import pytest

from hacienda_gpt.crawler.teac import (
    DYCTEA_NOT_FOUND_MARKER,
    TEACCrawler,
    parse_teac_criterio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "teac"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_returns_none_for_not_found_page() -> None:
    html = _read("criterio_not_found.html")
    assert DYCTEA_NOT_FOUND_MARKER in html
    assert parse_teac_criterio(html, criterio_id=42) is None


def test_parse_extracts_fields_from_table_layout() -> None:
    metadata = parse_teac_criterio(_read("criterio_tabla.html"), criterio_id=10)

    assert metadata is not None
    assert metadata["source_authority"] == "TEAC"
    assert metadata["criterio_id"] == 10
    assert metadata["numero_resolucion"] == "00/04582/2022/00/00"
    assert metadata["sala"] == "Vocalía Duodécima"
    assert metadata["fecha_resolucion"] == "2023-06-20"
    assert metadata["sentido"] == "estimatoria_parcial"
    assert metadata["vinculante"] is True
    assert metadata["concepto"].startswith("Impuesto sobre el Valor Añadido")
    assert "art. 92" in metadata["precepto"]
    assert "deducción del IVA" in metadata["criterio"]


def test_parse_extracts_fields_from_dl_layout() -> None:
    metadata = parse_teac_criterio(_read("criterio_dl.html"), criterio_id=11)

    assert metadata is not None
    assert metadata["numero_resolucion"] == "00/00123/2024/00/00"
    assert metadata["sala"] == "TEAR de Madrid - Sala Segunda"
    assert metadata["fecha_resolucion"] == "2024-03-05"
    assert metadata["sentido"] == "desestimatoria"
    assert metadata["vinculante"] is False
    assert metadata["concepto"].startswith("IRPF")
    assert metadata["norma"] == "Ley 35/2006"


def test_parse_falls_back_to_regex_for_numero_when_label_missing() -> None:
    html = "<html><body><p>Resolución 00/09999/2021/00/00 confirmando criterio anterior.</p></body></html>"
    metadata = parse_teac_criterio(html, criterio_id=99)
    assert metadata is not None
    assert metadata["numero_resolucion"] == "00/09999/2021/00/00"


def test_parse_returns_none_when_marker_is_present_even_with_partial_content() -> None:
    html = "<html><body><p>El criterio no existe.</p><table><tr><th>Sala</th><td>X</td></tr></table></body></html>"
    assert parse_teac_criterio(html, criterio_id=1) is None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serves DYCTEA-shaped responses from local fixtures.

    Maps the id query param to a specific fixture; everything else returns the
    "criterio no existe" stub. Mimics the production behaviour closely enough
    that the spider can drive the full code path end-to-end.
    """

    fixtures: dict[int, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if "id=" not in self.path:
            self.send_response(400)
            self.end_headers()
            return
        try:
            criterio_id = int(self.path.split("id=")[1].split("&")[0])
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        fixture = self.fixtures.get(criterio_id, "criterio_not_found.html")
        body = (FIXTURES / fixture).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return  # silence test output


@pytest.fixture
def fixture_server() -> tuple[str, threading.Thread, http.server.HTTPServer]:
    port = _free_port()
    handler = _FixtureHandler
    handler.fixtures = {10: "criterio_tabla.html", 11: "criterio_dl.html"}
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", thread, server
    server.shutdown()
    thread.join(timeout=2)


def test_crawler_persists_only_existing_criteria(
    tmp_path: Path,
    fixture_server: tuple[str, threading.Thread, http.server.HTTPServer],
) -> None:
    base_url, _thread, _server = fixture_server
    pytest.importorskip("scrapy")
    # The repo conftest stubs scrapy for the CI-lite path; this integration test
    # needs the real package. Skip when the stub is active.
    import scrapy as _scrapy

    if not hasattr(_scrapy, "__path__"):
        pytest.skip("scrapy is stubbed by conftest; real package required for this integration test")
    from scrapy.crawler import CrawlerProcess

    snapshot_date = "2026-05-28"
    process = CrawlerProcess(
        settings={
            "TELNETCONSOLE_ENABLED": False,
            "LOG_ENABLED": False,
            "DOWNLOAD_DELAY": 0,
            "ROBOTSTXT_OBEY": False,
        }
    )
    process.crawl(
        TEACCrawler,
        folder=str(tmp_path),
        start_id=9,
        end_id=12,
        snapshot_date=snapshot_date,
        base_url=base_url,
    )
    process.start(install_signal_handlers=False)

    snapshot_dir = tmp_path / snapshot_date
    saved_jsons = sorted(snapshot_dir.glob("*.json"))
    saved_htmls = sorted(snapshot_dir.glob("*.html"))

    # Only ids 10 and 11 had fixtures mapped; 9 and 12 returned the "no existe" stub.
    assert {p.name for p in saved_jsons} == {"criterio_10.json", "criterio_11.json"}
    assert {p.name for p in saved_htmls} == {"criterio_10.html", "criterio_11.html"}

    metadata_10 = json.loads((snapshot_dir / "criterio_10.json").read_text(encoding="utf-8"))
    assert metadata_10["numero_resolucion"] == "00/04582/2022/00/00"
    assert metadata_10["vinculante"] is True
    assert metadata_10["fecha_resolucion"] == "2023-06-20"


@pytest.mark.skipif(
    os.environ.get("TEAC_LIVE_SMOKE") != "1",
    reason="Set TEAC_LIVE_SMOKE=1 to run a single live request against DYCTEA.",
)
def test_live_smoke_against_dyctea() -> None:
    """Optional live check: fetch one id from the real DYCTEA host.

    This test is skipped by default because the CI environment may not have
    network access to *.hacienda.gob.es. Run locally with:

        TEAC_LIVE_SMOKE=1 poetry run pytest tests/test_teac_crawler.py::test_live_smoke_against_dyctea -v
    """
    import urllib.request

    req = urllib.request.Request(
        "https://serviciostelematicosext.hacienda.gob.es/TEAC/DYCTEA/criterio.aspx?id=1",
        headers={"User-Agent": "hacienda-asesor/0.1 smoke"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    # The real endpoint either returns a criterion page or the not-found stub;
    # both confirm the host is reachable and the URL pattern is correct.
    assert resp.status == 200
    assert "DYCTEA" in body or DYCTEA_NOT_FOUND_MARKER in body
