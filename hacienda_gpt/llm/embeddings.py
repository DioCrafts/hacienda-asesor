"""Embedding model factory — the single source of truth for the embedder.

Both indexing (``hacienda_gpt.processor.document_loader``) and querying
(``hacienda_gpt.llm.chain`` and ``hacienda_gpt.cli.benchmark_retrieval``)
build their embedder here so the FAISS index and the query path always share
the same vector space. Mixing embedders (e.g. indexing with a local model but
querying with OpenAI) silently breaks similarity search because the vectors
live in incompatible spaces.

The default is the local, multilingual ``Qwen/Qwen3-Embedding-8B`` model. It
ranked #1 on the MMTEB multilingual leaderboard, covers 100+ languages
(Spanish included) and runs offline, so it incurs no per-call API cost. The
backend is selected via the ``EMBEDDER`` / ``EMBEDDING_MODEL`` environment
variables (see ``hacienda_gpt.settings``).

Backends are imported lazily so that selecting one never forces the heavy
dependencies of the others to be installed.
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from hacienda_gpt import settings

_HUGGINGFACE_ALIASES = {"qwen3", "qwen", "huggingface", "hf"}


def create_embeddings(embedder: str | None = None, *, model: str | None = None) -> Embeddings:
    """Return the configured embedding model.

    Args:
        embedder: Override for ``settings.EMBEDDER``. One of ``qwen3``,
            ``openai`` or ``gpt4all``. Defaults to ``settings.EMBEDDER``.
        model: Override for ``settings.EMBEDDING_MODEL`` (a HuggingFace repo
            id). Only used by the local HuggingFace backend.

    Raises:
        ValueError: If ``embedder`` is not a recognised backend.
    """
    name = (embedder or settings.EMBEDDER).strip().lower()

    if name in _HUGGINGFACE_ALIASES:
        return _huggingface_embeddings(model or settings.EMBEDDING_MODEL)
    if name == "openai":
        from langchain_openai import OpenAIEmbeddings

        from hacienda_gpt.utils import get_openai_api_key

        return OpenAIEmbeddings(api_key=get_openai_api_key())
    if name == "gpt4all":
        from langchain_community.embeddings import GPT4AllEmbeddings

        return GPT4AllEmbeddings()

    raise ValueError(f"Unknown EMBEDDER={name!r}; expected one of: qwen3, openai, gpt4all")


def _huggingface_embeddings(model_name: str) -> Embeddings:
    """Build a local sentence-transformers embedder via langchain-huggingface."""
    from langchain_huggingface import HuggingFaceEmbeddings

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
