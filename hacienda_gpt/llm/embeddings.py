"""Embedding model factory — the single source of truth for the embedder.

Both indexing (``hacienda_gpt.processor.document_loader``) and querying
(``hacienda_gpt.llm.chain`` and ``hacienda_gpt.cli.benchmark_retrieval``) build
their embedder here so the FAISS index and the query path always share the same
vector space. Mixing embedders would silently break similarity search.

The app uses a single local, multilingual embedder: ``Qwen/Qwen3-Embedding-8B``
(#1 on MMTEB, 100+ languages including Spanish, no per-call API cost). Override
the model with ``EMBEDDING_MODEL`` (any sentence-transformers-compatible repo
id); see ``hacienda_gpt.settings``.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from hacienda_gpt import settings


def create_embeddings(model: str | None = None) -> Embeddings:
    """Return the local sentence-transformers embedder (via langchain-huggingface).

    Args:
        model: Override for ``settings.EMBEDDING_MODEL`` (a HuggingFace repo id).
    """
    # Imported lazily so importing this module never forces the heavy
    # sentence-transformers stack to be installed (e.g. for unit tests).
    from langchain_huggingface import HuggingFaceEmbeddings

    model_name = model or settings.EMBEDDING_MODEL

    model_kwargs: dict = {"device": settings.EMBEDDING_DEVICE}
    if settings.EMBEDDING_DIM:
        # Matryoshka (MRL) truncation: shrink the vector (e.g. 4096 -> 1024) to
        # trade a little quality for a smaller, faster FAISS index.
        model_kwargs["truncate_dim"] = int(settings.EMBEDDING_DIM)

    encode_kwargs = {"normalize_embeddings": settings.EMBEDDING_NORMALIZE}
    query_encode_kwargs = {"normalize_embeddings": settings.EMBEDDING_NORMALIZE}
    if "qwen" in model_name.lower():
        # Qwen3-Embedding is instruction-aware: queries use a task prompt
        # (shipped in the model's sentence-transformers config) while the
        # indexed documents do not. Omitting it costs ~1-5% retrieval quality.
        query_encode_kwargs["prompt_name"] = "query"

    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs=model_kwargs,
        encode_kwargs=encode_kwargs,
        query_encode_kwargs=query_encode_kwargs,
    )
