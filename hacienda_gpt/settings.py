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
#
# Measured A/B against the full 11 k-doc corpus (15-scenario fictional-user
# battery): +2 abstain→cited rescues, -1 cited→abstain regression, net +1
# cited, latency ~2× per query (6.5 s → 12.3 s median). Zero hallucinations
# in both modes. The rescues are substantively meaningful (plusvalía
# devolución + ISD donaciones — concrete tax advice users actually need);
# the regression is "abstain" rather than wrong advice. Net positive for a
# fiscal advisor where correctness >> latency, so we enable by default.
# Override with ``RERANKER_ENABLED=false`` to force the dense-only path
# (e.g. for latency benchmarks against the old baseline).
RERANKER_ENABLED = _env_bool("RERANKER_ENABLED", default=True)
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "data/models/qwen3-reranker-mlx-bf16")
# After reranking, keep the top-K documents to send to the LLM. The retriever's
# ``TOP_K`` controls first-stage recall (how many docs reach the reranker);
# this controls precision of what the LLM finally sees. Raised from 5 to 8
# (2026-06-02) to ATTACK FABRICATION: when the LLM was given only the top-5
# reranked chunks for under-specified queries (e.g. "reducción 95% empresa
# familiar"), it fell back to general knowledge and produced fluent-but-
# ungrounded answers (faith=0 with confident tone). 8 chunks widens the
# material the LLM has, reducing the temptation to fill gaps. Cost: +50%
# context tokens to the LLM, +5-10% latency. Acceptable for fiscal advisor
# where hallucination is strictly forbidden.
RERANKER_TOP_K = int(os.environ.get("RERANKER_TOP_K", "8"))
# Reranker first-stage k: when reranker is enabled we bump the FAISS retriever
# top-k so the reranker has more candidates to reorder. Raised from 20 to 30
# alongside the RERANKER_TOP_K bump — the reranker needs proportionally more
# raw recall to keep choosing 8 *good* chunks instead of 5.
RERANKER_FIRST_STAGE_K = int(os.environ.get("RERANKER_FIRST_STAGE_K", "30"))

# --- BM25 hybrid retrieval --------------------------------------------------
# Sparse BM25 retriever combined with the dense FAISS retriever via
# Reciprocal Rank Fusion (LangChain's ``EnsembleRetriever``). Targets queries
# that mention concrete entity names — ``modelo 721``, ``Ley 7/2012``,
# ``STC 182/2021`` — where token-overlap signal beats semantic similarity.
#
# **Disabled by default** based on measured A/B against the 15-scenario
# fictional-user battery on the full 11 k-doc corpus:
#
# * cited count: 8 (reranker-only) → 7 (BM25 + reranker), **net -1**;
# * latency: 12 s → 18 s median (+50 %, on top of the +10 GB build RAM);
# * BM25 *broke* the reranker's biggest rescue (``iivtnu_devolucion``)
#   because RRF mixed in topic-tangential docs that mention ``Tribunal
#   Constitucional`` and demoted the plusvalía-specific STCs;
# * the lexical queries we expected to rescue (Beckham, STC 8/2022) were
#   not rescued — those failures are **semantic** (the corpus uses
#   "régimen impatriados" not "Beckham") and **coverage** gaps that BM25
#   can't bridge.
#
# Kept in code as an opt-in for future re-evaluation against a proper
# golden test set (50–100 expert-annotated query-answer pairs) where the
# wins on entity-name queries may show up at scale. Enable with
# ``BM25_ENABLED=true``.
BM25_ENABLED = _env_bool("BM25_ENABLED", default=False)
# Relative weights for Reciprocal Rank Fusion: ``(dense_weight, bm25_weight)``.
# Default 0.6 / 0.4 biases slightly toward dense (which handles semantic
# queries) while letting BM25 dominate on exact-reference queries. Adjust
# with the retrieval benchmark once a golden set exists.
BM25_DENSE_WEIGHT = float(os.environ.get("BM25_DENSE_WEIGHT", "0.6"))
BM25_BM25_WEIGHT = float(os.environ.get("BM25_BM25_WEIGHT", "0.4"))
# First-stage k for BM25 — matched to the dense retriever's k so RRF is fair.
BM25_K = int(os.environ.get("BM25_K", "20"))

# --- Contextual embeddings (Anthropic September 2024) -----------------------
# At index time, prepend each chunk with a 1-sentence contextual prefix
# generated by a local Qwen3 instruct model. The intuition: a chunk that
# reads "El plazo es de 30 días hábiles" is unanchored — the embedder
# cannot disambiguate which deadline (ISD donaciones, IVA, modelo 720…).
# The contextual prefix supplies the anchor, so colloquial queries
# ("¿plazo donaciones?", "¿régimen Beckham?") align with chunks whose
# raw text uses the formal terminology ("art 9.1.a LIRPF", "art 93
# LIRPF"). Anthropic reported 35-50 % retrieval-failure reduction on
# their benchmarks.
#
# Implementation note: changing this flag (or the model / max-tokens)
# **invalidates the entire FAISS index** via the pipeline fingerprint —
# the chunks under it are embeddings of a different text. Off by default
# so existing indexes stay valid; flip to ``true`` and run the processor
# with ``--full --overwrite-output`` to rebuild.
CONTEXTUAL_EMBEDDINGS_ENABLED = _env_bool("CONTEXTUAL_EMBEDDINGS_ENABLED", default=False)
# Local MLX bf16 model that generates the prefix. Qwen3-1.7B is the
# sweet spot: Qwen3-0.6B echoes the prompt schema instead of filling it,
# Qwen3-4B + would make the full-corpus pass too slow.
CONTEXTUAL_MODEL_PATH = os.environ.get(
    "CONTEXTUAL_MODEL_PATH", "data/models/qwen3-1.7b-instruct-mlx-bf16"
)
# Per-chunk generation budget. 80 tokens is enough for a single Spanish
# sentence (~30 words) plus end-of-turn marker; smaller values risk
# truncating, larger ones inflate ingest wall-clock without quality gain.
CONTEXTUAL_MAX_TOKENS = int(os.environ.get("CONTEXTUAL_MAX_TOKENS", "80"))

# Local MLX model used as the **judge** for RAGAS-based RAG evaluation
# (``scripts/ragas_eval.py``). Decoupled from ``CONTEXTUAL_MODEL_PATH``
# so we can use a larger / newer instruction-tuned checkpoint for the
# judge — quality matters at judge time (structured-JSON outputs, yes/no
# verdicts), and the volume is tiny (~15-50 scenarios per eval). The
# default points at Qwen3-4B-Instruct-2507 (July-2025 refresh): native
# non-thinking mode, IFEval 83.4, ~8 GB on disk in bf16. Override with
# ``RAGAS_JUDGE_MODEL_PATH`` to A/B against a different judge.
RAGAS_JUDGE_MODEL_PATH = os.environ.get(
    "RAGAS_JUDGE_MODEL_PATH", "data/models/qwen3-4b-instruct-2507-mlx-bf16"
)
# Generation budget for the judge. RAGAS faithfulness emits one JSON
# object listing every statement in the answer with its verdict —
# answers with many statements need a generous budget to avoid
# mid-object truncation that breaks the parser.
RAGAS_JUDGE_MAX_TOKENS = int(os.environ.get("RAGAS_JUDGE_MAX_TOKENS", "1024"))

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
