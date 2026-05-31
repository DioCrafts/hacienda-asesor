"""Unit tests for the source-aware preprocessor registry.

Two layers of coverage here:

* **Registry mechanics** — registration ordering, first-match-wins probe,
  duplicate-id rejection, ``reset_registry`` for test isolation. These
  run anywhere and don't need real files.
* **BOE splitter unit-level checks** — using a synthetic minimal BOE
  HTML built in-memory so the test is deterministic and fast. The
  real-file smoke test on the 43 MB BOE-A-2023-26631 lives in the
  ``scripts/`` directory because it takes minutes and needs Docling.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.documents import Document

from hacienda_gpt.processor.strategies import (
    install_default_strategies,
    register,
    registered_strategies,
    reset_registry,
    select_strategy,
)
from hacienda_gpt.processor.strategies.base import IngestStrategy


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _ClaimByExtensionStrategy(IngestStrategy):
    """Test-only strategy: claims files matching a given extension."""

    def __init__(self, strategy_id: str, extension: str) -> None:
        self.strategy_id = strategy_id
        self._extension = extension

    def probe(self, file_path: str) -> bool:
        return file_path.endswith(self._extension)

    def parse(self, file_path: str):
        return [
            Document(
                page_content=f"parsed by {self.strategy_id}",
                metadata={
                    "source_file": file_path,
                    "chunk_id": f"{self.strategy_id}-0",
                    "_strategy_id": self.strategy_id,
                },
            )
        ]


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Every test starts with a clean registry — strategies registered in
    one test never leak into another."""
    reset_registry()
    yield
    reset_registry()


# --------------------------------------------------------------------------- #
# Registry mechanics
# --------------------------------------------------------------------------- #


def test_register_then_select_returns_registered_strategy() -> None:
    s = _ClaimByExtensionStrategy("test-html-v1", ".html")
    register(s)

    selected = select_strategy("/some/file.html")

    assert selected is s


def test_select_strategy_raises_when_no_strategy_claims_file() -> None:
    # Empty registry: nothing claims anything.
    with pytest.raises(RuntimeError, match="No ingest strategy claimed"):
        select_strategy("/x.txt")


def test_probe_first_match_wins_in_registration_order() -> None:
    first = _ClaimByExtensionStrategy("specialised", ".html")
    second = _ClaimByExtensionStrategy("fallback", ".html")
    register(first)
    register(second)

    # Both claim the file; first registered wins.
    assert select_strategy("/x.html") is first


def test_duplicate_strategy_id_is_rejected_loudly() -> None:
    register(_ClaimByExtensionStrategy("dup-id", ".a"))

    with pytest.raises(ValueError, match="already registered"):
        register(_ClaimByExtensionStrategy("dup-id", ".b"))


def test_reset_registry_clears_all_strategies() -> None:
    register(_ClaimByExtensionStrategy("x", ".x"))
    register(_ClaimByExtensionStrategy("y", ".y"))
    assert len(registered_strategies()) == 2

    reset_registry()

    assert registered_strategies() == []


def test_install_default_strategies_is_idempotent() -> None:
    install_default_strategies()
    first = [s.strategy_id for s in registered_strategies()]
    install_default_strategies()
    second = [s.strategy_id for s in registered_strategies()]

    assert first == second
    # Default fallback must be registered last so it doesn't shadow
    # specialised strategies.
    assert second[-1] == "default-docling-v1"


# --------------------------------------------------------------------------- #
# BOE splitter — synthetic-file unit checks
# --------------------------------------------------------------------------- #


def _minimal_boe_html(n_rows: int) -> str:
    """Build a small BOE-shaped HTML with one big table inside a
    ``<div class="bloque">``. Used by the splitter tests below — we DO
    NOT want to load the 43 MB real file here, just enough structure
    for the splitter to exercise its paths."""
    rows = "".join(
        f"<tr><td>row {i}</td><td>value {i}</td></tr>" for i in range(n_rows)
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head><title>BOE-A-2099-99999 Test consolidado</title></head>
<body>
  <div class="bloque">
    <h2>Anexo I</h2>
    <table>
      <thead><tr><th>col1</th><th>col2</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body>
</html>"""


def test_boe_probe_claims_only_large_boe_html(tmp_path: Path) -> None:
    """Probe must require BOTH the BOE-A title marker AND size > 4 MB."""
    from hacienda_gpt.processor.strategies.boe import BoeConsolidadoTableStrategy

    strategy = BoeConsolidadoTableStrategy()

    small_boe = tmp_path / "small.html"
    small_boe.write_text(_minimal_boe_html(5), encoding="utf-8")
    assert not strategy.probe(str(small_boe))  # too small (~few KB)

    # Inflate to >5 MB by padding the body so the size gate passes.
    padding = '<div class="filler">' + ("x" * 5_000_000) + "</div>"
    big_boe = tmp_path / "big.html"
    big_boe.write_text(_minimal_boe_html(100) + padding, encoding="utf-8")
    assert strategy.probe(str(big_boe))

    # Non-BOE big HTML: title doesn't match → not claimed.
    non_boe = tmp_path / "non-boe.html"
    non_boe.write_text(
        f"<!DOCTYPE html><html><head><title>Some other doc</title></head><body>{padding}</body></html>",
        encoding="utf-8",
    )
    assert not strategy.probe(str(non_boe))


def test_boe_probe_rejects_non_html_extensions(tmp_path: Path) -> None:
    from hacienda_gpt.processor.strategies.boe import BoeConsolidadoTableStrategy

    strategy = BoeConsolidadoTableStrategy()
    # Big enough byte-wise but wrong extension.
    p = tmp_path / "big.txt"
    p.write_bytes(b"<title>BOE-A-2099-99999</title>" + b"x" * (5 * 1024 * 1024))
    assert not strategy.probe(str(p))


def test_split_block_tables_keeps_thead_on_each_fragment() -> None:
    """The whole point of the row-bound split is that every sub-table
    remains independently intelligible — i.e. each sub-fragment carries
    the column headers."""
    from hacienda_gpt.processor.strategies.boe import _split_block_tables

    # Build a block with 1500 rows; MAX_ROWS_PER_TABLE=500 → 3 sub-tables.
    block = """
    <div class="bloque">
      <h2>Tabla</h2>
      <table>
        <thead><tr><th>A</th><th>B</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """.format(
        rows="".join(f"<tr><td>{i}</td><td>{i*2}</td></tr>" for i in range(1500))
    )

    fragments = _split_block_tables(block)

    # 3 fragments from the table split + (optionally) a residual block.
    assert len(fragments) >= 3
    table_fragments = [f for f in fragments if "<th>A</th>" in f]
    assert len(table_fragments) == 3
    # Each table fragment carries the header AND the partial-range
    # annotation we emit for debuggability.
    for f in table_fragments:
        assert "<th>A</th>" in f
        assert "<th>B</th>" in f
        assert "filas" in f  # "filas X-Y de 1500"


def test_split_block_tables_passes_small_block_through_unchanged() -> None:
    """A block whose tables are all under the row threshold should be
    returned as a single fragment — no splitting overhead for normal docs."""
    from hacienda_gpt.processor.strategies.boe import _split_block_tables

    block = """
    <div class="bloque">
      <p>Small block</p>
      <table>
        <thead><tr><th>col</th></tr></thead>
        <tbody>
          <tr><td>r1</td></tr>
          <tr><td>r2</td></tr>
        </tbody>
      </table>
    </div>
    """

    fragments = _split_block_tables(block)

    assert len(fragments) == 1
