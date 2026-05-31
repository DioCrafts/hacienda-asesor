import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0"))

# --- Embeddings -------------------------------------------------------------
# The app uses a single local embedder via MLX (Apple Silicon native, bf16,
# no quantization). The converted model directory contains both the bf16
# weights for the MLX runtime AND the HuggingFace tokenizer files that the
# Docling HybridChunker uses for token-aware chunk splitting — so a single
# ``EMBEDDING_MODEL`` path serves indexing, querying, and chunking. Convert a
# new model with ``scripts/convert_to_mlx.py``.
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "data/models/qwen3-emb-mlx-bf16"
)
# How many chunks the embedder processes per forward pass. Default 32 is the
# empirical optimum on M-series for Qwen3-Embedding-0.6B. Counter-intuitively,
# larger batches slow things down because the underlying tokenizer pads each
# batch to its longest member without length-sorting; bigger batches end up
# containing more length variance, wasting attention compute on padding
# tokens. Override only after measuring on your corpus + hardware.
EMBEDDING_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "32"))
# Hard cap on per-input tokens at encode time. Qwen3-Embedding ships with
# ``max_seq_length=32768`` (its native context). Capping at 512 matches the
# chunker's budget and protects throughput from any chunk that accidentally
# slips past the chunker (defence-in-depth). The cap is **defensive only**:
# benchmarks on the current corpus show no measurable speedup from it,
# because attention cost depends on actual sequence length, not on the cap.
EMBEDDING_MAX_SEQ_LENGTH = int(os.environ.get("EMBEDDING_MAX_SEQ_LENGTH", "512"))

# --- Reranker (Qwen3-Reranker on MLX) ---------------------------------------
# Cross-encoder pass after first-stage dense retrieval. Scores (query, doc)
# pairs directly and re-orders the candidate set, surfacing the docs that
# actually answer the query (vs. docs that are merely topically similar).
# Opt-in: indexing pipelines don't need it, and we want a clean A/B against
# the dense-only baseline before enabling it by default.
RERANKER_ENABLED = _env_bool("RERANKER_ENABLED", default=False)
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "data/models/qwen3-reranker-mlx-bf16")
# After reranking, keep the top-K documents to send to the LLM. The retriever's
# ``TOP_K`` controls first-stage recall (how many docs reach the reranker);
# this controls precision of what the LLM finally sees. 5 is a balance —
# enough for the LLM to triangulate, few enough to fit context budget.
RERANKER_TOP_K = int(os.environ.get("RERANKER_TOP_K", "5"))
# Reranker first-stage k: when reranker is enabled we bump the FAISS retriever
# top-k so the reranker has more candidates to reorder. The reranker is much
# better at picking the truly relevant ones, so widening recall here is cheap.
RERANKER_FIRST_STAGE_K = int(os.environ.get("RERANKER_FIRST_STAGE_K", "20"))

FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", ".faiss")
FAISS_TRUSTED_INDEX = _env_bool("FAISS_TRUSTED_INDEX", default=False)
MEMORY_KEY = os.environ.get("MEMORY_KEY", "chat_history")
TOP_K = int(os.environ.get("TOP_K", "3"))

DECISION_DEBUG_MODE = _env_bool("DECISION_DEBUG_MODE", default=False)
DECISION_STATE_DB_PATH = os.environ.get("DECISION_STATE_DB_PATH", "./data/decision_state.sqlite3")
UI_USE_API = _env_bool("UI_USE_API", default=False)
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")

GROUNDING_MIN_CITATIONS = int(os.environ.get("GROUNDING_MIN_CITATIONS", "1"))
GROUNDING_SNIPPET_CHARS = int(os.environ.get("GROUNDING_SNIPPET_CHARS", "240"))

# --- Retrieval thresholds ---------------------------------------------------
# Cosine-similarity floor applied by the EmbeddingsFilter compressor in the
# retrieval chain. The historical values (0.82 / 0.75) were tuned for OpenAI
# embeddings; with the local Qwen3-Embedding (normalized cosine) those floors
# over-filter and can starve the grounding gate, making it abstain even when
# retrieval is good. The defaults below are lowered to bias toward recall.
# Calibrate empirically per corpus/embedder with:
#   uv run python -m hacienda_gpt.cli.benchmark_retrieval
# and override via env when you have measured numbers.
RETRIEVAL_DECISION_THRESHOLD = float(os.environ.get("RETRIEVAL_DECISION_THRESHOLD", "0.45"))
RETRIEVAL_EXPLAIN_THRESHOLD = float(os.environ.get("RETRIEVAL_EXPLAIN_THRESHOLD", "0.35"))
