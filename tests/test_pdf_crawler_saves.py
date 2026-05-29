"""AgenciaTributariaPDFCrawler now writes the fetched PDF bytes to disk.

The legacy spider yielded a FilesPipeline item that was never wired up, so no
PDF was ever persisted. These tests pin the direct-write behaviour, the
non-PDF guard, content dedupe and path-traversal safety.
"""

from __future__ import annotations

from pathlib import Path

from hacienda_gpt.crawler.crawlers import AgenciaTributariaPDFCrawler


class _FakeResponse:
    def __init__(self, url: str, body: bytes, content_type: bytes | None = b"application/pdf") -> None:
        self.url = url
        self.body = body
        self.headers = {} if content_type is None else {"Content-Type": content_type}


def _crawler(tmp_path: Path) -> AgenciaTributariaPDFCrawler:
    return AgenciaTributariaPDFCrawler(folder=str(tmp_path), snapshot_date="2026-01-01")


def test_parse_pdf_writes_pdf_bytes(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)
    crawler.parse_pdf(_FakeResponse("https://sede.agenciatributaria.gob.es/manual.pdf", b"%PDF-1.7 contenido"))

    saved = list(Path(crawler.folder).glob("*.pdf"))
    assert len(saved) == 1
    assert saved[0].name == "manual.pdf"
    assert saved[0].read_bytes().startswith(b"%PDF")


def test_parse_pdf_skips_non_pdf_body(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)
    # A Chromium-rendered viewer (HTML) must not be saved as a PDF.
    crawler.parse_pdf(_FakeResponse("https://x/doc.pdf", b"<html>visor</html>"))
    assert list(Path(crawler.folder).glob("*")) == []


def test_parse_pdf_deduplicates_identical_content(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)
    crawler.parse_pdf(_FakeResponse("https://x/a.pdf", b"%PDF dup"))
    crawler.parse_pdf(_FakeResponse("https://x/b.pdf", b"%PDF dup"))
    assert len(crawler.seen_content_hashes) == 1
    assert len(list(Path(crawler.folder).glob("*.pdf"))) == 1


def test_generate_pdf_path_is_traversal_safe(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)
    path = crawler._generate_pdf_path("https://x/a/b/../../../../etc/evil.pdf")
    assert ".." not in path
    assert Path(path).resolve().parent == Path(crawler.folder).resolve()
