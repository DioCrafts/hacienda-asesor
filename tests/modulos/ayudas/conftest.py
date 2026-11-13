"""Every test of the module runs with its own store directory and with the network features off."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "ayudas"


@pytest.fixture(autouse=True)
def _aislado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    store = tmp_path / "ayudas_store"
    monkeypatch.setenv("AYUDAS_DATA_DIR", str(store))
    for var in ("AYUDAS_BOE_AUTOMATICO", "AYUDAS_BDNS", "AYUDAS_CLASIFICADOR_LLM"):
        monkeypatch.delenv(var, raising=False)

    def sin_red(*args: object, **kwargs: object) -> None:
        raise AssertionError("los tests no deben acceder a la red")

    monkeypatch.setattr("hacienda_gpt.modulos.ayudas.revision._get", sin_red)
    monkeypatch.setattr("hacienda_gpt.modulos.ayudas.bdns.descargar", sin_red)
    yield store
