"""Qwen3-Reranker cross-encoder on MLX, wrapped as a LangChain compressor.

The reranker scores ``(query, document)`` pairs directly (cross-encoder),
unlike the embedder which compares two independent embeddings (bi-encoder).
The score comes from the next-token logits for ``"yes"`` vs ``"no"`` in a
specific instruction-following prompt — see the Qwen3-Reranker model card.

This module slots into the retrieval chain as a
:class:`langchain_core.documents.compressor.BaseDocumentCompressor`: given a
list of retrieved documents and the original query, it re-orders them by
relevance and returns the top-K. A regular ``ContextualCompressionRetriever``
wraps it transparently, so the rest of the chain doesn't need to know.

**Why MLX bf16, no quantization**: matches the project's embedder backend
(same hardware path, same precision policy). Latency ~1s for 20 documents on
M-series; cheap enough to add to every ``/qa`` request.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import Callbacks
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from pydantic import ConfigDict


# Qwen3-Reranker scoring protocol — verbatim from the model card.
# https://huggingface.co/Qwen/Qwen3-Reranker-0.6B
_SYSTEM_PROMPT = (
    'Judge whether the Document meets the requirements based on the Query and '
    'the Instruct provided. Note that the answer can only be "yes" or "no".'
)
_PREFIX = "<|im_start|>system\n" + _SYSTEM_PROMPT + "<|im_end|>\n<|im_start|>user\n"
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_DEFAULT_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)


# Process-level cache so multiple MLXReranker instances share a single loaded
# model (1.1 GB) and tokenizer instead of paying the load cost per request.
_MODEL_CACHE: dict[str, tuple[Any, Any, int, int]] = {}


def _get_model(model_path: str) -> tuple[Any, Any, int, int]:
    """Return ``(model, tokenizer, yes_id, no_id)``, loading once per path."""
    if model_path in _MODEL_CACHE:
        return _MODEL_CACHE[model_path]
    from mlx_lm import load

    model, tokenizer = load(model_path)
    yes_id = tokenizer.encode("yes", add_special_tokens=False)[0]
    no_id = tokenizer.encode("no", add_special_tokens=False)[0]
    _MODEL_CACHE[model_path] = (model, tokenizer, yes_id, no_id)
    return _MODEL_CACHE[model_path]


class MLXReranker(BaseDocumentCompressor):
    """Score ``(query, doc)`` pairs with Qwen3-Reranker on MLX, keep top-K.

    Pydantic v2 model — picked up by the LangChain
    ``ContextualCompressionRetriever`` without further glue.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_path: str
    top_k: int = 5
    instruction: str = _DEFAULT_INSTRUCTION

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Callbacks | None = None,
    ) -> Sequence[Document]:
        if not documents:
            return []
        scores = self._score(query, [d.page_content for d in documents])
        # Sort by relevance descending, keep the top-K and stamp the score on
        # metadata so downstream callers (logs, UI) can audit ranking.
        ranked = sorted(zip(documents, scores), key=lambda pair: pair[1], reverse=True)
        result: list[Document] = []
        for doc, score in ranked[: self.top_k]:
            metadata = dict(doc.metadata or {})
            metadata["_rerank_score"] = float(score)
            result.append(doc.model_copy(update={"metadata": metadata}))
        return result

    async def acompress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Callbacks | None = None,
    ) -> Sequence[Document]:
        # The MLX scoring path is synchronous; there's no async win for
        # in-process MLX inference. Delegating to the sync method keeps the
        # interface complete without pretending to be async.
        return self.compress_documents(documents, query, callbacks=callbacks)

    # ------------------- internals --------------------------------------- #

    def _format_prompt(self, query: str, doc: str) -> str:
        body = f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: {doc}"
        return _PREFIX + body + _SUFFIX

    def _score(self, query: str, docs: list[str]) -> list[float]:
        import mlx.core as mx

        model, tokenizer, yes_id, no_id = _get_model(self.model_path)
        scores: list[float] = []
        for doc in docs:
            prompt = self._format_prompt(query, doc)
            ids = tokenizer.encode(prompt, add_special_tokens=False)
            x = mx.array(ids)[None, :]
            logits = model(x)[0, -1, :]
            # Softmax over (no, yes) → probability of "yes". Mirrors the
            # reference scoring code in the Qwen3-Reranker model card.
            pair = mx.stack([logits[no_id], logits[yes_id]])
            scores.append(float(mx.softmax(pair)[1].item()))
        return scores
