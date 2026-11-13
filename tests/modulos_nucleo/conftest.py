from collections.abc import Iterator

import pytest

from hacienda_gpt.modulos import registro


@pytest.fixture(autouse=True)
def registro_limpio() -> Iterator[None]:
    """Every test starts and ends with the real (discovered) registry; fakes are installed per test."""
    registro.reiniciar()
    yield
    registro.reiniciar()
