"""Contextual chunk enrichment (Anthropic-style, September 2024).

Prepends a 1–2 sentence "contextual" prefix to each chunk before embedding,
so the embedder sees the chunk's place in its parent document — not just the
raw text. The intuition (from Anthropic's paper): a chunk that reads "El
plazo es de 30 días hábiles" is unanchored; an embedder cannot tell whether
it is the donation deadline, the VAT refund deadline, or a hundred others.
The contextual prefix supplies that anchor, so semantic queries (``¿plazo
donaciones?``, ``¿plazo modelo 720?``) align with the right chunk.

Implementation differs from Anthropic's published recipe in one way: they
use Claude Haiku with prompt-caching over the **full parent document** as
the LLM's context window. We instead pass only the chunk's own Docling
metadata (heading hierarchy, source URL, section) — much cheaper, scales
to 650 k+ chunks on a single Mac, and good enough in practice because
Docling already extracted the structural signals we need.

Model: a small generative Qwen3 fine-tuned for instruction following,
converted to MLX bf16 (no quantization — project policy). Qwen3-0.6B is
too small (echoes the schema labels instead of filling them); Qwen3-1.7B
follows instructions reliably while staying fast (~0.5–0.8 s per chunk on
M-series, ~1 GB peak RAM beyond model weights). Bigger models would be
better but the latency hit at full-corpus scale isn't worth it.

Used by ``document_loader.process_documents`` when
``CONTEXTUAL_EMBEDDINGS_ENABLED`` is set. Changing the model, prompt, or
max-tokens bumps the pipeline fingerprint and forces a clean re-ingest.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# Conservative system instruction. The model is asked to ADD context, not
# paraphrase the chunk — we explicitly forbid copying the literal content
# because small Qwen3 sizes (≤1.7 B) have a bias toward echoing the input
# when uncertain. Output is constrained to one short Spanish sentence so
# the cost (latency + token budget at embed time) stays predictable.
_PROMPT_TEMPLATE = """Resume en UNA SOLA FRASE (máximo 20 palabras, español) qué tipo de norma fiscal regula el siguiente fragmento. Reglas estrictas:

1. Menciona ÚNICAMENTE conceptos, impuestos, modelos, leyes o sentencias que aparezcan LITERALMENTE en el fragmento o en su título.
2. NO inventes referencias. Si el fragmento no menciona "modelo 720", no escribas "modelo 720". Si no menciona "Beckham", no escribas "Beckham".
3. NO inventes números de artículo. Cita un número de artículo SOLO si aparece literalmente en el fragmento. Los números válidos son tipo "30", "9.1.a", "93", "20.2.c" — formatos como "225-24-144-1" no existen en derecho español; nunca los inventes.
4. Si dudas, sé GENÉRICO ("resolución administrativa sobre IRPF", "doctrina TEAC sobre IVA", "artículo del reglamento de IVA").
5. No copies el contenido del fragmento; añade contexto sobre QUÉ TIPO de norma es.

Título del documento: {title}
Sección: {section}

Fragmento:
{content}

Frase de contexto (sólo conceptos presentes en el fragmento):"""


_INSTANCE_LOCK = threading.Lock()
_INSTANCE: "MLXContextualizer | None" = None


def get_contextualizer(model_path: str, max_tokens: int = 80) -> "MLXContextualizer":
    """Return the process-level singleton contextualizer.

    Lazy-loaded behind a lock so a burst of concurrent first-time requests
    (e.g. a parallelised ingest's bg checkpoint thread + a smoke test in
    the main thread) doesn't load the 3 GB MLX model twice.
    """
    global _INSTANCE
    if _INSTANCE is not None and _INSTANCE.model_path == model_path:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None and _INSTANCE.model_path == model_path:
            return _INSTANCE
        _INSTANCE = MLXContextualizer(model_path=model_path, max_tokens=max_tokens)
        return _INSTANCE


class MLXContextualizer:
    """Wraps a small generative MLX Qwen3 for chunk contextualisation."""

    def __init__(self, *, model_path: str, max_tokens: int = 80) -> None:
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Contextualizer model not found at {model_path!r}. "
                f"Convert with `python -m mlx_lm convert --hf-path "
                f"Qwen/Qwen3-1.7B --mlx-path {model_path} --dtype bfloat16`."
            )
        self.model_path = model_path
        self.max_tokens = max_tokens

        # Lazy import keeps the module importable on machines without mlx
        # (tests, CI on Linux).
        from mlx_lm import load

        logger.info("Loading contextualizer model %s ...", model_path)
        self._model, self._tokenizer = load(str(path))
        logger.info("Contextualizer model loaded")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def contextualize(self, chunks: Sequence[Document]) -> list[Document]:
        """Return a NEW list of Documents, each with a contextual prefix
        prepended to ``page_content``. The original chunk's metadata is
        copied with an added ``contextual_prefix`` field for auditing.

        Defensive: if generation fails on any single chunk the original
        chunk is passed through unchanged (with a warning) — we never want
        a contextualisation hiccup to abort the whole ingest.
        """
        out: list[Document] = []
        for chunk in chunks:
            try:
                prefix = self._generate_one(chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Contextualizer failed on chunk (%s); passing through unchanged.",
                    exc,
                )
                out.append(chunk)
                continue
            if not prefix:
                out.append(chunk)
                continue
            new_text = f"[Contexto: {prefix}]\n\n{chunk.page_content}"
            new_meta: dict[str, Any] = dict(chunk.metadata or {})
            new_meta["contextual_prefix"] = prefix
            out.append(Document(page_content=new_text, metadata=new_meta))
        return out

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_prompt(self, chunk: Document) -> str:
        metadata = chunk.metadata or {}
        return _PROMPT_TEMPLATE.format(
            title=str(metadata.get("title") or metadata.get("source_url") or "?")[:200],
            section=str(metadata.get("section") or "—")[:200],
            content=chunk.page_content[:2000],  # cap to keep prompt budget bounded
        )

    def _generate_one(self, chunk: Document) -> str:
        from mlx_lm import generate

        prompt = self._build_prompt(chunk)
        messages = [{"role": "user", "content": prompt}]
        # ``enable_thinking=False`` is critical for Qwen3 — by default the
        # model emits a ``<think>...</think>`` chain-of-thought block that
        # eats the entire ``max_tokens`` budget before producing real
        # output. We don't need the reasoning, just the prefix.
        formatted = self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        raw = generate(
            self._model,
            self._tokenizer,
            prompt=formatted,
            max_tokens=self.max_tokens,
            verbose=False,
        )
        # mlx-lm's ``generate`` returns just the assistant response (the
        # part after the chat template's ``<|im_start|>assistant``).
        # Strip whitespace + any trailing ``<|im_end|>`` artefact.
        return _clean_output(raw)


def _clean_output(raw: str) -> str:
    """Trim whitespace, strip Qwen3 end-of-turn markers, drop trailing
    fragments. Empty string is returned if the cleanup leaves nothing
    usable — callers treat empty as "skip contextualisation for this
    chunk" and pass through the original."""
    if not raw:
        return ""
    text = raw.strip()
    # Drop end-of-turn markers if any leaked through.
    for marker in ("<|im_end|>", "</s>", "<|endoftext|>"):
        if marker in text:
            text = text.split(marker)[0].strip()
    # Some models prepend "Contexto:" even though the prompt already said
    # "Contexto:" — strip duplication.
    for prefix in ("Contexto:", "contexto:", "[Contexto]", "[CONTEXTO]"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # First line only — extra paragraphs would defeat the point.
    text = text.split("\n", 1)[0].strip()
    return text


__all__ = ["MLXContextualizer", "get_contextualizer"]
