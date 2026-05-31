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

import threading
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
# Guarded by a lock so a burst of concurrent first-time requests (UI cold
# start + API cold start landing on the same process) does **not** load the
# 1.1 GB MLX weights twice — both threads would observe an empty cache,
# both would call ``load(...)``, and the second one would silently overwrite
# the first while we waste memory + GPU init time.
_MODEL_CACHE: dict[str, tuple[Any, Any, int, int]] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _get_model(model_path: str) -> tuple[Any, Any, int, int]:
    """Return ``(model, tokenizer, yes_id, no_id)``, loading once per path.

    Thread-safe via double-checked locking: the fast path (cache hit) skips
    the lock entirely so request-time scoring stays lock-free; only the
    one-time cold load pays for the lock.
    """
    cached = _MODEL_CACHE.get(model_path)
    if cached is not None:
        return cached
    with _MODEL_CACHE_LOCK:
        # Re-check after acquiring the lock: another thread may have loaded
        # the model while we were waiting.
        cached = _MODEL_CACHE.get(model_path)
        if cached is not None:
            return cached
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
        """Score every ``(query, doc)`` pair in a single batched MLX forward.

        Why batched: the previous loop ran one forward per doc, which on
        M-series wasted ~3 s per 20-doc rerank because each forward had its
        own GPU dispatch overhead. Measured speedup vs the per-doc loop on
        20 mixed Spanish chunks: **~3 ×** wall time, with rank order
        identical and max per-doc score delta below ``8e-3`` (bf16 noise).

        Why **right** padding (and not left): mlx-lm's qwen3 forward does
        not accept an attention mask, so left-padding would let pad tokens
        be seen by the model's causal attention at the last position
        (position ``-1``) and contaminate the score. With right-padding the
        last *real* token of each row sits at a row-specific index
        ``k_i``; under the causal mask, the model's prediction at position
        ``k_i`` attends only to positions ``≤ k_i``, all of which are real
        prompt tokens. We index each row at its own ``k_i`` to recover the
        exact same logits the serial path would have produced.
        """
        import mlx.core as mx

        model, tokenizer, yes_id, no_id = _get_model(self.model_path)
        # ``mlx_lm`` wraps the HF tokenizer; the underlying ``_tokenizer`` is
        # the callable HF one. Force right-padding so the **last real token**
        # sits at each row's ``attention_mask.sum() - 1`` position.
        inner = getattr(tokenizer, "_tokenizer", tokenizer)
        inner.padding_side = "right"

        prompts = [self._format_prompt(query, doc) for doc in docs]
        batch = inner(
            prompts,
            padding=True,
            truncation=True,
            max_length=2048,
            return_tensors="np",
        )
        input_ids = mx.array(batch["input_ids"])
        # ``last_pos[i]`` = index of the final real token of prompt i.
        last_pos = batch["attention_mask"].sum(axis=1) - 1
        logits = model(input_ids)  # (B, T, V)

        # Gather per-row logits at the row's own last-real-token position.
        # No mx.gather one-liner; the explicit list comp is cheap (B<=50).
        yes_logits = mx.stack(
            [logits[i, int(last_pos[i]), yes_id] for i in range(len(docs))]
        )
        no_logits = mx.stack(
            [logits[i, int(last_pos[i]), no_id] for i in range(len(docs))]
        )
        paired = mx.stack([no_logits, yes_logits], axis=1)  # (B, 2)
        probs = mx.softmax(paired, axis=1)  # (B, 2)
        return probs[:, 1].tolist()
