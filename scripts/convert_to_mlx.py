"""Convert a HuggingFace Qwen3-Embedding model to local MLX bf16.

The project runs its embedder on Apple Silicon via the ``mlx-embeddings``
package. That package can only load models that were already converted from
PyTorch / safetensors to MLX format. This script does the conversion **without
quantization** (a hard project rule — see memory/feedback-no-quantization).

Usage:
    uv run python scripts/convert_to_mlx.py \
        --hf-path Qwen/Qwen3-Embedding-0.6B \
        --mlx-path data/models/qwen3-emb-mlx-bf16

The default target path matches ``settings.EMBEDDING_MODEL``, so a run with
no flags produces exactly the artifact the rest of the app expects.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _verify_no_quantization(mlx_path: Path) -> None:
    """Refuse to leave behind a quantized artifact even if the converter
    silently inserted quantization (defensive: we've seen MLX repos on HF
    where the ``-fp16`` suffix in the name hides 4-bit weights).
    """
    config_path = mlx_path / "config.json"
    if not config_path.exists():
        print(f"WARNING: no config.json at {mlx_path}; skipping quantization check.")
        return
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"FATAL: could not parse {config_path}: {exc}")
    quant = cfg.get("quantization")
    if quant:
        sys.exit(
            f"FATAL: converted model at {mlx_path} reports quantization={quant!r}. "
            "Project policy forbids any quantization. Re-run convert without -q."
        )
    dtype = cfg.get("torch_dtype")
    if dtype not in (None, "bfloat16", "float16", "float32"):
        sys.exit(
            f"FATAL: converted model has unexpected torch_dtype={dtype!r}. "
            "Expected bfloat16 / float16 / float32. Aborting."
        )
    print(f"OK: no quantization, torch_dtype={dtype}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hf-path",
        default="Qwen/Qwen3-Embedding-0.6B",
        help="HuggingFace repo id or local path of the source model.",
    )
    parser.add_argument(
        "--mlx-path",
        default="data/models/qwen3-emb-mlx-bf16",
        help="Local directory to write the converted MLX model into.",
    )
    parser.add_argument(
        "--dtype",
        default="bfloat16",
        choices=["bfloat16", "float16", "float32"],
        help="Storage dtype for the converted weights. bf16 is the project default.",
    )
    args = parser.parse_args()

    target = Path(args.mlx_path)
    if target.exists() and any(target.iterdir()):
        sys.exit(
            f"FATAL: {target} already exists and is non-empty. Move/delete it first "
            "to avoid silently mixing converted artifacts."
        )

    # Import inside main() so `--help` works on platforms without mlx.
    try:
        from mlx_embeddings.convert import convert  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.exit(
            f"FATAL: mlx-embeddings is not installed ({exc}). Install with "
            "`uv pip install mlx mlx-embeddings` on an Apple Silicon Mac."
        )

    print(f"Converting {args.hf_path} -> {target} (dtype={args.dtype}, NO quantization)...")
    # Critical: do NOT pass any quantization kwargs. The mlx_embeddings.convert
    # function accepts ``quantize=False`` (or omits it) to skip quantization.
    convert(
        hf_path=args.hf_path,
        mlx_path=str(target),
        dtype=args.dtype,
        quantize=False,
    )

    _verify_no_quantization(target)
    print(f"Done. Set EMBEDDING_MODEL={target} to use this model.")


if __name__ == "__main__":
    main()
