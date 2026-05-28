from __future__ import annotations

from pathlib import Path

from hacienda_gpt.crawler.boletin_autonomico import (
    BOCM_MADRID,
    BUNDLED_SPECS,
    parse_listing,
)

FIXTURES = Path(__file__).parent / "fixtures" / "boletines"


def test_bundled_specs_cover_three_pilot_ccaa() -> None:
    keys = set(BUNDLED_SPECS.keys())
    assert keys == {"madrid_bocm", "andalucia_boja", "cataluna_dogc"}


def test_parse_listing_extracts_results_from_bocm_layout() -> None:
    html = (FIXTURES / "bocm_listing.html").read_text(encoding="utf-8")
    rows = parse_listing(BOCM_MADRID, html)

    assert len(rows) == 2
    first, second = rows
    assert first["title"].startswith("Ley 6/2023")
    assert first["date_raw"] == "28/12/2023"
    assert first["link"].endswith("/Ley-6-2023.html")
    assert first["link"].startswith("https://www.bocm.es")
    assert first["boletin"] == "madrid_bocm"
    assert first["ccaa_ine"] == "MD"

    assert "IRPF" in second["title"]
    assert second["date_raw"] == "12/06/2024"


def test_parse_listing_ignores_unrelated_blocks() -> None:
    html = (FIXTURES / "bocm_listing.html").read_text(encoding="utf-8")
    rows = parse_listing(BOCM_MADRID, html)
    titles = [row["title"] for row in rows]
    assert all("Bloque que no debe matchear" not in title for title in titles)


def test_default_terms_target_the_three_autonomic_tributes() -> None:
    for spec in BUNDLED_SPECS.values():
        joined = " ".join(spec.default_terms).lower()
        # Either explicit acronyms or full names should be present.
        assert any(token in joined for token in ("isd", "sucesiones"))
        assert any(token in joined for token in ("itp", "transmisiones"))
        assert any(token in joined for token in ("irpf",))
