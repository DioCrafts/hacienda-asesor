from __future__ import annotations

from pathlib import Path

from hacienda_gpt.cli.boe_watch import _resolve_target


def test_resolve_target_reuses_recorded_path(tmp_path: Path) -> None:
    # Steady state: once we know where the document lives, every later change
    # overwrites that exact path so the manifest re-chunks it in place.
    recorded = tmp_path / "2026-06-01" / "boe-seed" / "BOE-A-2006-20764.html"
    local = {"path": str(recorded)}
    assert _resolve_target(tmp_path, "boe-seed", "BOE-A-2006-20764", local) == recorded


def test_resolve_target_overwrites_existing_seed_copy(tmp_path: Path) -> None:
    # First time boe_watch touches a doc that boe_seed already wrote: overwrite
    # the seeded file (a dated snapshot path) instead of creating a new one,
    # so the manifest sees a modification, not a duplicate.
    seed = tmp_path / "2026-06-01" / "boe-leyes-fundamentales" / "BOE-A-2006-20764.html"
    seed.parent.mkdir(parents=True)
    seed.write_text("old", encoding="utf-8")

    target = _resolve_target(tmp_path, "boe-leyes-fundamentales", "BOE-A-2006-20764", {})
    assert target == seed


def test_resolve_target_stable_fallback_when_nothing_on_disk(tmp_path: Path) -> None:
    # Nothing seeded yet: write to a stable, non-dated location future runs reuse
    # (no per-run snapshot rotation -> no accumulating duplicates).
    target = _resolve_target(tmp_path, "boe-seed", "BOE-A-2006-20764", {})
    assert target == tmp_path / "boe-watch" / "boe-seed" / "BOE-A-2006-20764.html"
