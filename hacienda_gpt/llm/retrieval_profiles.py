from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalProfile:
    name: str
    similarity_threshold: float
    metadata_filter: dict[str, object] | None


def _fiscal_year_filter(fiscal_year: int | None) -> dict[str, object] | None:
    if fiscal_year is None:
        return None
    # Works with metadata precomputed in chunks; if absent, filter is best-effort.
    return {"fiscal_year": fiscal_year}


def build_decision_profile(
    fiscal_year: int | None = None,
    metadata_filter: dict[str, object] | None = None,
) -> RetrievalProfile:
    merged: dict[str, object] = {}
    fy = _fiscal_year_filter(fiscal_year)
    if fy:
        merged.update(fy)
    if metadata_filter:
        merged.update(metadata_filter)
    return RetrievalProfile(
        name="decision_retriever",
        similarity_threshold=0.82,
        metadata_filter=merged,
    )


def build_explain_profile(
    fiscal_year: int | None = None,
    metadata_filter: dict[str, object] | None = None,
) -> RetrievalProfile:
    merged: dict[str, object] = {}
    fy = _fiscal_year_filter(fiscal_year)
    if fy:
        merged.update(fy)
    if metadata_filter:
        merged.update(metadata_filter)
    return RetrievalProfile(
        name="explain_retriever",
        similarity_threshold=0.75,
        metadata_filter=merged,
    )
