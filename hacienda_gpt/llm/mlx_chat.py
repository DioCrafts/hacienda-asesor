"""LangChain-compatible chat model backed by a local MLX Qwen3.

Used by the RAGAS evaluation pipeline to run LLM-as-judge **locally** —
without any OpenAI API call — over the existing ``mlx_lm`` runtime that
ships the contextualizer model. Faithfulness, context-precision and
answer-relevancy all decompose into short yes/no judgements and small
structured outputs, which a 1.7 B-parameter model can handle reliably.

The wrapper implements just enough of LangChain's ``BaseChatModel`` for
RAGAS's ``LangchainLLMWrapper`` to drive it: a synchronous ``_generate``
that takes a list of messages, formats them with the model's chat
template, runs ``mlx_lm.generate`` and returns a single ``ChatGeneration``.
Streaming / async / tool calls aren't needed — RAGAS only ever asks for
a complete response per turn.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict


_MODEL_CACHE: dict[str, tuple[Any, Any]] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _load_model(model_path: str) -> tuple[Any, Any]:
    """Process-level singleton load. RAGAS will instantiate the chat
    wrapper many times across a single eval run; this avoids re-loading
    the 3 GB MLX weights on every instantiation."""
    cached = _MODEL_CACHE.get(model_path)
    if cached is not None:
        return cached
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(model_path)
        if cached is not None:
            return cached
        from mlx_lm import load

        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(
                f"MLX chat model directory not found at {model_path!r}. "
                f"Convert with `python -m mlx_lm convert --hf-path "
                f"Qwen/Qwen3-1.7B --mlx-path {model_path} --dtype bfloat16`."
            )
        model, tokenizer = load(str(path))
        _MODEL_CACHE[model_path] = (model, tokenizer)
        return _MODEL_CACHE[model_path]


def _messages_to_chat_template(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
    """Translate LangChain BaseMessage instances into the role/content
    dicts that Qwen3's ``apply_chat_template`` expects. RAGAS sends
    System + Human messages; we collapse everything down to the standard
    ``system`` / ``user`` / ``assistant`` triple."""
    out: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            role = "system"
        elif isinstance(m, HumanMessage):
            role = "user"
        elif isinstance(m, AIMessage):
            role = "assistant"
        else:
            # Fallback: treat anything else as a user turn — RAGAS doesn't
            # use tool / function messages in its judge prompts.
            role = "user"
        out.append({"role": role, "content": m.content if isinstance(m.content, str) else str(m.content)})
    return out


class MLXChatLLM(BaseChatModel):
    """Local Qwen3 (MLX bf16) wrapped as a LangChain chat model.

    Configured via ``model_path``, ``max_tokens`` and ``temperature``.
    Thinking mode is **disabled** in the chat template — RAGAS judges
    want a direct answer, not a chain-of-thought block that consumes the
    token budget before producing the verdict.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_path: str
    max_tokens: int = 512
    temperature: float = 0.0  # RAGAS judges should be deterministic.

    @property
    def _llm_type(self) -> str:
        return "mlx-chat"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        from mlx_lm import generate

        model, tokenizer = _load_model(self.model_path)
        chat = _messages_to_chat_template(messages)
        formatted = tokenizer.apply_chat_template(
            chat,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        # ``stop`` sequences aren't supported by mlx_lm.generate, but we
        # trim them post-hoc below for compatibility.
        raw = generate(
            model,
            tokenizer,
            prompt=formatted,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            verbose=False,
        )
        text = (raw or "").strip()
        for marker in ("<|im_end|>", "</s>", "<|endoftext|>"):
            if marker in text:
                text = text.split(marker)[0].strip()
        if stop:
            for s in stop:
                if s in text:
                    text = text.split(s)[0].strip()

        message = AIMessage(content=text)
        return ChatResult(generations=[ChatGeneration(message=message)])


__all__ = ["MLXChatLLM"]
