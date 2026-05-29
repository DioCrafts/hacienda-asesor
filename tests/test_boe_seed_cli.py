from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import boe_seed

_CATALOG = {
    "description": "test",
    "documents": [
        {
            "id": "BOE-X-0001",
            "topic": "modelo_xxx",
            "label": "Test BOE one",
            "subdir": "boe-test",
        },
        {
            "id": "BOE-X-0002",
            "topic": "modelo_xxx",
            "label": "Test BOE two",
            "subdir": "boe-test",
        },
    ],
}


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_CATALOG), encoding="utf-8")
    return path


def test_boe_seed_dry_run_lists_targets(catalog_path: Path, tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        boe_seed.cli,
        [
            "--catalog",
            str(catalog_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--snapshot-date",
            "2026-01-15",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "BOE-X-0001" in result.output
    assert "BOE-X-0002" in result.output
    # Dry-run must not create files.
    assert not (tmp_path / "out" / "2026-01-15" / "boe-test").exists()


def test_boe_seed_real_run_writes_files_and_manifest(
    catalog_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured_urls: list[str] = []

    class _FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req, timeout):  # noqa: ANN001
        captured_urls.append(req.full_url)
        body = f"<html><title>{req.full_url}</title></html>".encode()
        return _FakeResponse(body)

    monkeypatch.setattr(boe_seed.urllib.request, "urlopen", fake_urlopen)

    runner = CliRunner()
    result = runner.invoke(
        boe_seed.cli,
        [
            "--catalog",
            str(catalog_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--snapshot-date",
            "2026-01-15",
        ],
    )
    assert result.exit_code == 0, result.output

    out_dir = tmp_path / "out" / "2026-01-15" / "boe-test"
    assert (out_dir / "BOE-X-0001.html").exists()
    assert (out_dir / "BOE-X-0002.html").exists()

    manifest_path = tmp_path / "out" / "2026-01-15" / "boe-seed-manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["snapshot_date"] == "2026-01-15"
    ids = [doc["id"] for doc in manifest["documents"]]
    assert ids == ["BOE-X-0001", "BOE-X-0002"]
    for doc in manifest["documents"]:
        assert doc["bytes"] > 0

    # We must call BOE's act.php (consolidated) endpoint, not the original
    # diario_boe one — that contract is what guarantees the indexed text
    # reflects current law.
    assert all("buscar/act.php?id=" in url for url in captured_urls)


def test_boe_seed_retries_on_transient_failure(catalog_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    attempts: list[int] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return b"<html><title>ok</title></html>"

    def flaky_urlopen(req, timeout):  # noqa: ANN001
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("simulated network blip")
        return _FakeResponse()

    monkeypatch.setattr(boe_seed.urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(boe_seed.time, "sleep", lambda _s: None)

    catalog_one = tmp_path / "single.json"
    catalog_one.write_text(
        json.dumps(
            {
                "documents": [
                    {"id": "BOE-X-9999", "label": "only", "subdir": "boe-flaky"},
                ]
            }
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        boe_seed.cli,
        [
            "--catalog",
            str(catalog_one),
            "--output-dir",
            str(tmp_path / "out"),
            "--snapshot-date",
            "2026-01-15",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "2026-01-15" / "boe-flaky" / "BOE-X-9999.html").exists()
    assert len(attempts) == 2, "expected one retry on the transient TimeoutError"
