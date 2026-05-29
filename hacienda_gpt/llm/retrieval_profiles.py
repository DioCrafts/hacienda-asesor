from __future__ import annotations

from dataclasses import dataclass

from hacienda_gpt import settings


@dataclass(frozen=True)
class RetrievalProfile:
    name: str
    similarity_threshold: float
    metadata_filter: dict[str, object] | None


def build_decision_profile(metadata_filter: dict[str, object] | None = None) -> RetrievalProfile:
    return RetrievalProfile(
        name="decision_retriever",
        similarity_threshold=settings.RETRIEVAL_DECISION_THRESHOLD,
        metadata_filter=dict(metadata_filter) if metadata_filter else None,
    )


def build_explain_profile(metadata_filter: dict[str, object] | None = None) -> RetrievalProfile:
    return RetrievalProfile(
        name="explain_retriever",
        similarity_threshold=settings.RETRIEVAL_EXPLAIN_THRESHOLD,
        metadata_filter=dict(metadata_filter) if metadata_filter else None,
    )
