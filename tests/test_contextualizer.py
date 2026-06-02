"""Unit tests for the MLX contextualizer.

The real Qwen3-1.7B model load is expensive (~3 GB on disk) and Apple-Silicon
only, so we stub ``mlx_lm.load`` and ``mlx_lm.generate``. The cleanup helper
and the per-chunk wrapping logic are exercised directly without touching
MLX. The behaviour-level verification of generation quality (e.g. that the
prefix doesn't echo the chunk) runs on the target hardware as a smoke test,
not in CI.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from langchain_core.documents import Document


# --------------------------------------------------------------------------- #
# Stub helpers
# --------------------------------------------------------------------------- #


class _FakeTokenizer:
    """Stand-in for mlx-lm's tokenizer wrapper. Records the prompts it sees
    so tests can assert the prompt format."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, enable_thinking):
        self.calls.append(
            {
                "messages": messages,
                "tokenize": tokenize,
                "add_generation_prompt": add_generation_prompt,
                "enable_thinking": enable_thinking,
            }
        )
        # Return the user content (no real chat template wrapping needed).
        return messages[0]["content"]


@pytest.fixture
def stub_mlx_lm(monkeypatch, tmp_path: Path):
    """Stub ``mlx_lm.load`` + ``mlx_lm.generate`` with a deterministic
    fake. The fake generator returns the LITERAL string we configure per
    test via the ``generator_output`` fixture parameter."""

    fake_lm = types.ModuleType("mlx_lm")

    # Defaults — tests override via ``fake_lm.generator_output``.
    fake_lm.generator_output = "régimen especial impatriados (Beckham), tributación IRPF"

    def fake_load(path):
        return object(), _FakeTokenizer()

    def fake_generate(model, tokenizer, *, prompt, max_tokens, verbose):
        tokenizer.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        return fake_lm.generator_output

    fake_lm.load = fake_load
    fake_lm.generate = fake_generate
    monkeypatch.setitem(sys.modules, "mlx_lm", fake_lm)

    # Also create a fake model directory so the constructor's path check passes.
    model_dir = tmp_path / "fake-model"
    model_dir.mkdir()

    from hacienda_gpt.processor import contextualizer as ctx

    # Clear the module-level singleton so each test starts fresh.
    ctx._INSTANCE = None
    return fake_lm, str(model_dir)


# --------------------------------------------------------------------------- #
# Output cleanup
# --------------------------------------------------------------------------- #


def test_clean_output_strips_eot_markers() -> None:
    from hacienda_gpt.processor.contextualizer import _clean_output

    assert _clean_output("foo bar<|im_end|>extra") == "foo bar"
    assert _clean_output("foo</s>baz") == "foo"
    assert _clean_output("foo<|endoftext|>") == "foo"


def test_clean_output_drops_redundant_prefix() -> None:
    from hacienda_gpt.processor.contextualizer import _clean_output

    assert _clean_output("Contexto: trata del IRPF") == "trata del IRPF"
    assert _clean_output("[Contexto] sobre el modelo 720") == "sobre el modelo 720"


def test_clean_output_keeps_only_first_line() -> None:
    """A multi-paragraph answer is reduced to the first line — extra
    paragraphs would balloon the embedded text without adding signal."""
    from hacienda_gpt.processor.contextualizer import _clean_output

    raw = "Primera línea.\nSegunda línea extra.\nTercera."
    assert _clean_output(raw) == "Primera línea."


def test_clean_output_empty_input_returns_empty() -> None:
    from hacienda_gpt.processor.contextualizer import _clean_output

    assert _clean_output("") == ""
    assert _clean_output("   \n  ") == ""


# --------------------------------------------------------------------------- #
# Contextualize behaviour
# --------------------------------------------------------------------------- #


def test_contextualize_prepends_prefix_to_page_content(stub_mlx_lm) -> None:
    from hacienda_gpt.processor.contextualizer import MLXContextualizer

    _, model_dir = stub_mlx_lm
    ctx = MLXContextualizer(model_path=model_dir, max_tokens=80)

    docs = [
        Document(
            page_content="El plazo es de 30 días hábiles.",
            metadata={"title": "Modelo 651 ISD donaciones", "section": "Plazos"},
        )
    ]

    out = ctx.contextualize(docs)

    assert len(out) == 1
    new = out[0]
    # The default fake generator returns a Beckham/IRPF prefix; the wrapper
    # prepends it with a "[Contexto: ...]" marker for auditability.
    assert "[Contexto:" in new.page_content
    assert new.page_content.endswith("El plazo es de 30 días hábiles.")
    # Original metadata preserved + ``contextual_prefix`` recorded.
    assert new.metadata["title"] == "Modelo 651 ISD donaciones"
    assert "contextual_prefix" in new.metadata


def test_contextualize_passes_through_on_generator_error(stub_mlx_lm) -> None:
    """A buggy / OOM contextualizer must NOT abort the whole ingest; the
    affected chunks are passed through unchanged so the rest of the batch
    is persisted normally."""
    fake_lm, model_dir = stub_mlx_lm

    def boom(*args, **kwargs):
        raise RuntimeError("simulated generation failure")

    fake_lm.generate = boom

    from hacienda_gpt.processor.contextualizer import MLXContextualizer

    ctx = MLXContextualizer(model_path=model_dir, max_tokens=80)
    docs = [Document(page_content="x", metadata={"title": "t"})]

    out = ctx.contextualize(docs)

    # Pass-through — content unchanged, no prefix.
    assert len(out) == 1
    assert out[0].page_content == "x"
    assert "contextual_prefix" not in out[0].metadata


def test_contextualize_empty_generator_output_passes_through(stub_mlx_lm) -> None:
    fake_lm, model_dir = stub_mlx_lm
    fake_lm.generator_output = ""

    from hacienda_gpt.processor.contextualizer import MLXContextualizer

    ctx = MLXContextualizer(model_path=model_dir, max_tokens=80)
    docs = [Document(page_content="x", metadata={"title": "t"})]

    out = ctx.contextualize(docs)
    assert out[0].page_content == "x"


def test_missing_model_path_raises(stub_mlx_lm, tmp_path: Path) -> None:
    from hacienda_gpt.processor.contextualizer import MLXContextualizer

    with pytest.raises(FileNotFoundError, match="Contextualizer model not found"):
        MLXContextualizer(model_path=str(tmp_path / "missing"), max_tokens=80)


def test_thinking_disabled_in_chat_template(stub_mlx_lm) -> None:
    """Qwen3's thinking mode would eat the ``max_tokens`` budget with a
    chain-of-thought block before producing real output. The contextualizer
    must always call ``apply_chat_template(enable_thinking=False)``."""
    from hacienda_gpt.processor.contextualizer import MLXContextualizer

    _, model_dir = stub_mlx_lm
    ctx = MLXContextualizer(model_path=model_dir, max_tokens=80)
    ctx.contextualize([Document(page_content="x", metadata={"title": "t"})])

    template_call = ctx._tokenizer.calls[0]
    assert template_call["enable_thinking"] is False


def test_singleton_factory_reuses_loaded_model(stub_mlx_lm) -> None:
    """``get_contextualizer`` is a thread-safe lazy singleton — multiple
    calls with the same path must return the same instance, not reload
    the (3 GB) MLX weights."""
    from hacienda_gpt.processor.contextualizer import get_contextualizer

    _, model_dir = stub_mlx_lm
    a = get_contextualizer(model_dir)
    b = get_contextualizer(model_dir)
    assert a is b
