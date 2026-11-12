"""Corpus search behind the agent's ``buscar_fuentes`` tool.

Unlike the legacy ``/qa`` chain there is no LLM here: no MultiQuery fan-out and
no history-aware rewriting. The agent writes its own queries, reads the results
and searches again if they fall short — that *is* the query rewriting, and it
is driven by what the model actually found missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.documents import Document

from hacienda_gpt.agent.evidence import infer_authority, source_type_for


@dataclass(frozen=True)
class SearchFilters:
    source_type: str | None = None  # one of evidence.SOURCE_TYPES
    tributo: str | None = None
    jurisdiction_scope: str | None = None  # "estatal" | "autonomico:<código o nombre>"
    binding_only: bool = False

    def is_empty(self) -> bool:
        return not (self.source_type or self.tributo or self.jurisdiction_scope or self.binding_only)


class CorpusSearcher(Protocol):
    def search(self, query: str, *, k: int, filters: SearchFilters) -> list[Document]: ...


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return []


def build_filter_predicate(filters: SearchFilters) -> Callable[[dict[str, Any]], bool]:
    """Metadata predicate for ``filters``.

    ``source_type`` and ``binding_only`` are **strict**: a document whose
    authority is unknown is not doctrine, and one without an explicit
    ``vinculante=True`` is not binding. ``tributo`` and ``jurisdiction_scope``
    are **lenient** (a document that does not declare them is kept), because
    most of the corpus carries no such tag and a strict match would silently
    hide the state-wide law that applies everywhere.
    """
    tributo = (filters.tributo or "").strip().lower()
    scope = (filters.jurisdiction_scope or "").strip().lower()

    def predicate(metadata: dict[str, Any]) -> bool:
        if filters.source_type:
            if source_type_for(infer_authority(metadata), metadata) != filters.source_type:
                return False
        if filters.binding_only and metadata.get("vinculante") is not True:
            return False
        if tributo:
            declared = [
                item.lower()
                for key in ("covered_tributos", "tributos_detectados")
                for item in _as_list(metadata.get(key))
            ]
            if declared and not any(tributo in item or item in tributo for item in declared):
                return False
        if scope:
            declared_scope = str(metadata.get("jurisdiction_scope") or "").strip().lower()
            if declared_scope:
                if scope == "estatal":
                    if declared_scope.startswith("autonomico"):
                        return False
                elif declared_scope.startswith("autonomico") and declared_scope != scope:
                    # Another region's rules never apply; state-wide ones do.
                    return False
        return True

    return predicate


class FaissCorpusSearcher:
    """Dense FAISS search + optional cross-encoder rerank, with sanitization."""

    def __init__(self, vectorstore: Any, *, reranker: Any | None = None, first_stage_k: int = 30) -> None:
        self._vectorstore = vectorstore
        self._reranker = reranker
        self._first_stage_k = first_stage_k

    @classmethod
    def from_settings(cls) -> FaissCorpusSearcher:
        from langchain_community.vectorstores import FAISS

        from hacienda_gpt import settings
        from hacienda_gpt.llm.embeddings import create_embeddings

        if not settings.FAISS_TRUSTED_INDEX:
            raise RuntimeError(
                "Refusing to load FAISS index with dangerous deserialization. "
                "Set FAISS_TRUSTED_INDEX=true only for trusted local indexes."
            )
        vectorstore = FAISS.load_local(
            settings.FAISS_INDEX_PATH, create_embeddings(), allow_dangerous_deserialization=True
        )
        reranker = None
        if settings.RERANKER_ENABLED:
            from hacienda_gpt.llm.reranker import MLXReranker

            reranker = MLXReranker(model_path=settings.RERANKER_MODEL, top_k=settings.RERANKER_TOP_K)
        return cls(vectorstore, reranker=reranker, first_stage_k=settings.RERANKER_FIRST_STAGE_K)

    def search(self, query: str, *, k: int, filters: SearchFilters) -> list[Document]:
        from hacienda_gpt.llm.chain import _sanitize_context_documents

        first_stage_k = max(k, self._first_stage_k) if self._reranker is not None else k
        kwargs: dict[str, Any] = {"k": first_stage_k}
        if not filters.is_empty():
            kwargs["filter"] = build_filter_predicate(filters)
            # FAISS applies the filter AFTER fetching fetch_k neighbours; a
            # selective filter (e.g. only binding doctrine) needs a deep pool.
            kwargs["fetch_k"] = max(first_stage_k * 20, 500)
        docs = self._vectorstore.similarity_search(query, **kwargs)
        if self._reranker is not None and docs:
            docs = list(self._reranker.compress_documents(docs, query))
        # Retrieved text is untrusted input to the model: redact injection bait.
        return _sanitize_context_documents(list(docs[:k]))
