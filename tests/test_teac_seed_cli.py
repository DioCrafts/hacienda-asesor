from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import teac_seed

_LIST_PAGE_1 = """
<html><body>
<a href='criterio.aspx?id=54/00218/2024/00/0/1&amp;q=s%3d1%26'>One</a>
<a href='criterio.aspx?id=30/00962/2023/00/0/1&amp;q=s%3d1%26'>Two</a>
<a href='criterio.aspx?id=00/00636/2022/00/0/1&amp;q=s%3d1%26'>Three</a>
</body></html>
"""

_LIST_PAGE_2 = """
<html><body>
<a href='criterio.aspx?id=46/02639/2021/00/0/1&amp;q=s%3d1%26'>Four</a>
</body></html>
"""

_EMPTY_LIST_PAGE = "<html><body><p>Sin resultados</p></body></html>"

_CRITERIO_VALID = """
<html><body>
<table>
  <tr><th>Número de resolución</th><td>54/00218/2024/00/00</td></tr>
  <tr><th>Sala</th><td>Vocalía Quinta</td></tr>
  <tr><th>Fecha de la resolución</th><td>15/03/2024</td></tr>
  <tr><th>Concepto</th><td>IRPF</td></tr>
</table>
</body></html>
"""

_CRITERIO_NOT_FOUND = "<html><body>El criterio no existe en la base de datos.</body></html>"


def _build_fetcher(responses: dict[str, bytes]):
    def fetcher(url: str, **kwargs):
        for key, body in responses.items():
            if key in url:
                return body
        raise AssertionError(f"unexpected URL in test: {url}")

    return fetcher


def test_collect_criterio_ids_walks_until_empty():
    responses = {
        "pg=1": _LIST_PAGE_1.encode(),
        "pg=2": _LIST_PAGE_2.encode(),
        "pg=3": _EMPTY_LIST_PAGE.encode(),
    }
    ids = teac_seed.collect_criterio_ids(
        fd="01/01/2024", fh="31/12/2024", page_limit=5, fetcher=_build_fetcher(responses)
    )
    assert ids == [
        "54/00218/2024/00/0/1",
        "30/00962/2023/00/0/1",
        "00/00636/2022/00/0/1",
        "46/02639/2021/00/0/1",
    ]


def test_collect_dedups_when_pages_overlap():
    # Some date ranges return the same row across pages; we must dedupe but
    # preserve insertion order.
    responses = {
        "pg=1": _LIST_PAGE_1.encode(),
        "pg=2": _LIST_PAGE_1.encode(),  # exact same — pagination is over
    }
    ids = teac_seed.collect_criterio_ids(
        fd="01/01/2024", fh="31/12/2024", page_limit=5, fetcher=_build_fetcher(responses)
    )
    assert ids == [
        "54/00218/2024/00/0/1",
        "30/00962/2023/00/0/1",
        "00/00636/2022/00/0/1",
    ]


def test_fetch_criterio_writes_html_and_metadata(tmp_path: Path):
    target_dir = tmp_path / "teac"
    fetcher = _build_fetcher({"criterio.aspx?id=": _CRITERIO_VALID.encode()})
    html_path, metadata_path = teac_seed.fetch_criterio("54/00218/2024/00/0/1", target_dir, fetcher=fetcher)
    assert html_path.exists()
    assert metadata_path is not None and metadata_path.exists()
    metadata = json.loads(metadata_path.read_text())
    assert metadata["criterio_id"] == "54/00218/2024/00/0/1"
    assert metadata["source_authority"] == "TEAC"


def test_fetch_criterio_drops_not_existing(tmp_path: Path):
    target_dir = tmp_path / "teac"
    fetcher = _build_fetcher({"criterio.aspx?id=": _CRITERIO_NOT_FOUND.encode()})
    html_path, metadata_path = teac_seed.fetch_criterio("99/99999/2099/00/0/1", target_dir, fetcher=fetcher)
    # Both files must be absent — we don't want to index DYCTEA's
    # "criterio no existe" placeholder.
    assert not html_path.exists()
    assert metadata_path is None


def test_cli_end_to_end_with_mocked_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured: list[str] = []

    def fake_http_get(url: str, **kwargs):
        captured.append(url)
        if "criterios.aspx" in url and "pg=1" in url:
            return _LIST_PAGE_1.encode()
        if "criterios.aspx" in url and "pg=2" in url:
            return _EMPTY_LIST_PAGE.encode()
        if "criterio.aspx?id=" in url:
            return _CRITERIO_VALID.encode()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(teac_seed, "_http_get", fake_http_get)

    runner = CliRunner()
    result = runner.invoke(
        teac_seed.cli,
        [
            "--from-date",
            "2024-01-01",
            "--to-date",
            "2024-12-31",
            "--output-dir",
            str(tmp_path / "out"),
            "--snapshot-date",
            "2026-01-15",
            "--page-limit",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output

    teac_dir = tmp_path / "out" / "2026-01-15" / "teac"
    htmls = sorted(teac_dir.glob("*.html"))
    jsons = sorted(teac_dir.glob("*.json"))
    assert len(htmls) == 3
    assert len(jsons) == 3
    # Compound IDs survive on disk with '/' replaced by '_'.
    assert any("54_00218_2024_00_0_1" in p.name for p in htmls)

    manifest = json.loads((tmp_path / "out" / "2026-01-15" / "teac-seed-manifest.json").read_text())
    assert manifest["from_date"] == "01/01/2024"
    assert manifest["to_date"] == "31/12/2024"
    assert len(manifest["documents"]) == 3
    assert manifest["skipped_not_existing"] == []


def test_cli_validates_date_format():
    runner = CliRunner()
    result = runner.invoke(
        teac_seed.cli,
        ["--from-date", "not-a-date", "--to-date", "2024-12-31"],
    )
    assert result.exit_code != 0
    assert "Invalid date" in result.output or "Invalid" in result.output
