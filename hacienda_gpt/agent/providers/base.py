"""Provider-neutral contract for the tool-using agent.

The agent loop (:mod:`hacienda_gpt.agent.agent`) never talks to an SDK. It
drives a :class:`Conversation` obtained from an :class:`LLMProvider`, so the
same loop runs on Claude (default), OpenAI, or any local model served behind an
OpenAI-compatible endpoint (Ollama, LM Studio, ``mlx_lm.server``, vLLM…).

Each provider keeps the conversation in its *native* wire format. That matters:
Claude's assistant turns carry thinking blocks that must be replayed verbatim,
and OpenAI's carry ``tool_calls`` objects — flattening either into a common
shape and rebuilding it would lose information the API needs back.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal["end", "tool_use", "max_tokens", "refusal"]


class ProviderError(RuntimeError):
    """An LLM call failed after the SDK's own retries.

    ``retryable`` tells the caller whether trying again later can help (rate
    limit, 5xx, network) or not (bad request, auth, unknown model).
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Attachment:
    """A file the model must read itself: a PDF or an image (scans, photos)."""

    media_type: str  # "application/pdf", "image/png", "image/jpeg", "image/webp" or "image/gif"
    data: bytes
    filename: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call. ``input_schema`` is a JSON Schema object."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the provider could not parse the model's arguments (OpenAI-style
    # APIs deliver them as a JSON string). The agent answers such a call with an
    # error result instead of executing the tool on garbage.
    arguments_error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: dict[str, int] = field(default_factory=dict)


# Receives the model's visible text as it is generated, chunk by chunk.
TextCallback = Callable[[str], None]


class Conversation(Protocol):
    def step(self, *, allow_tools: bool = True, on_text: TextCallback | None = None) -> ModelTurn:
        """Ask the model for its next turn and record it in the history.

        With ``on_text`` the request is streamed and each text chunk is passed
        to the callback as it arrives; the returned turn is the same either way.
        """
        ...

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Answer every tool call of the last turn (all of them, in one go)."""
        ...

    def add_user_message(self, text: str) -> None: ...


class LLMProvider(Protocol):
    name: str
    model: str

    def start(
        self,
        *,
        system: str,
        tools: list[ToolSpec],
        history: list[dict[str, str]],
        user_message: str,
    ) -> Conversation: ...

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        """Single-shot call that returns an object matching ``schema``.

        ``attachments`` are PDFs or images sent alongside ``user`` for the model
        to read (it needs a vision-capable model).
        """
        ...


def normalize_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Reduce caller-supplied chat history to clean user/assistant text turns.

    Drops anything that is not a plain-text user/assistant message, and any
    leading assistant turns (every supported API requires the first message to
    come from the user).
    """
    cleaned: list[dict[str, str]] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content})
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


def rasterizar_pdf(data: bytes, *, max_pages: int, scale: float = 2.0) -> list[bytes]:
    """Render each page of a PDF as PNG, for backends that read images but not PDFs."""
    import io

    import pypdfium2

    pdf = pypdfium2.PdfDocument(data)
    try:
        pages: list[bytes] = []
        for index in range(min(len(pdf), max_pages)):
            buffer = io.BytesIO()
            pdf[index].render(scale=scale).to_pil().save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        return pages
    finally:
        pdf.close()
