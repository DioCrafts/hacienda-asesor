import os
from pathlib import Path


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
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "data/models/qwen3-emb-mlx-bf16")
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
CONTEXTUAL_MODEL_PATH = os.environ.get("CONTEXTUAL_MODEL_PATH", "data/models/qwen3-1.7b-instruct-mlx-bf16")
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
RAGAS_JUDGE_MODEL_PATH = os.environ.get("RAGAS_JUDGE_MODEL_PATH", "data/models/qwen3-4b-instruct-2507-mlx-bf16")
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
# Validation / repeal status of the decree-laws some parameters depend on (written by `proactive normas-check`).
NORM_STATUS_PATH = os.environ.get("NORM_STATUS_PATH", "./data/_estado_normas.json")
# Decision rules live at the repo root (sibling of the hacienda_gpt package).
# Resolve to an ABSOLUTE path so the engine loads them regardless of the process
# working directory (uvicorn/Streamlit may start elsewhere). A relative "rules"
# silently found nothing from any other CWD — and RuleSet(min_length=1) then
# raised — stalling every turn. Overridable via the RULES_DIR env var.
RULES_DIR = os.environ.get("RULES_DIR", str(Path(__file__).resolve().parent.parent / "rules"))
UI_USE_API = _env_bool("UI_USE_API", default=False)
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")

GROUNDING_MIN_CITATIONS = int(os.environ.get("GROUNDING_MIN_CITATIONS", "1"))
GROUNDING_SNIPPET_CHARS = int(os.environ.get("GROUNDING_SNIPPET_CHARS", "240"))

# --- API edge (auth + rate limiting) ----------------------------------------
# Both opt-in; defaults keep local dev and tests unauthenticated/unthrottled.
# Set HACIENDA_API_KEY (enforced via the X-API-Key header) and a positive
# API_RATE_LIMIT_PER_MIN before exposing the API beyond localhost — /qa spends
# OpenAI tokens and loads multi-GB models per process.
API_RATE_LIMIT_PER_MIN = int(os.environ.get("API_RATE_LIMIT_PER_MIN", "0"))

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

# --- Agent (tool-using advisor) ---------------------------------------------
# Which LLM drives the agent loop and the verifier:
#   anthropic (default) | openai | local (any OpenAI-compatible server).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
# Thinking depth / token spend for Claude. Legal reasoning over several sources
# is intelligence-sensitive, so the floor is "high" (Opus 5.5 would otherwise
# default to "medium"). low | medium | high | xhigh | max.
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "high")
# Re-run a request another Claude model when a safety classifier declines it
# (server-side fallback). Only applied to models that support it.
ANTHROPIC_REFUSAL_FALLBACK = _env_bool("ANTHROPIC_REFUSAL_FALLBACK", default=True)
OPENAI_AGENT_MODEL = os.environ.get("OPENAI_AGENT_MODEL", OPENAI_MODEL)
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")  # Ollama default
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "")
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "local")
LOCAL_LLM_TIMEOUT = float(os.environ.get("LOCAL_LLM_TIMEOUT", "300"))
# The verifier may run on a different provider/model than the agent (e.g. a
# cheaper one, or a different vendor for an independent read). Empty = same.
VERIFIER_PROVIDER = os.environ.get("VERIFIER_PROVIDER", "")
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL", "")
# Hard cap on model turns per question (each turn may issue several searches).
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
# How many times the agent may rewrite an answer the verifier rejected.
AGENT_MAX_REVISIONS = int(os.environ.get("AGENT_MAX_REVISIONS", "1"))
AGENT_SEARCH_DEFAULT_K = int(os.environ.get("AGENT_SEARCH_DEFAULT_K", "6"))
# Thinking depth for the fact extractor when it runs on Claude.
DECISION_EXTRACTOR_EFFORT = os.environ.get("DECISION_EXTRACTOR_EFFORT", "low")

# --- Doctrine graph ----------------------------------------------------------
# SQLite file built by `python -m hacienda_gpt.cli.doctrine_graph build`. When it
# exists the agent annotates every retrieved ruling/consulta with its validity
# (current, qualified, superseded, disputed); when it does not, nothing changes.
DOCTRINE_GRAPH_PATH = os.environ.get("DOCTRINE_GRAPH_PATH", "./data/doctrine_graph.sqlite3")

# --- Agent team (planner + researchers + critic) ----------------------------
# auto   : a single agent answers; the team is used only when the question spans
#          several taxes (per the planner) or the single agent hit conflicting or
#          superseded sources it did not resolve.
# always : every question goes through the team (slow and costly; for evaluation).
# never  : single agent only.
AGENT_TEAM_MODE = os.environ.get("AGENT_TEAM_MODE", "auto")
# Model turns each researcher / the critic may take.
SUBAGENT_MAX_STEPS = int(os.environ.get("SUBAGENT_MAX_STEPS", "4"))
# Run the two researchers concurrently (their LLM calls; corpus searches are serialised).
SUBAGENTS_PARALLEL = _env_bool("SUBAGENTS_PARALLEL", default=True)

# --- Notification delivery outside the app ----------------------------------
# All optional; an empty value keeps that channel off. There are no user
# accounts yet, so everything goes to ONE owner/operator destination (see
# hacienda_gpt/proactive/entrega.py). Secrets (the webhook URL, the SMTP
# password) must come from the environment, never from code.
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "")
# json : POST {"title": ..., "text": ...} (Slack-compatible and generic webhooks).
# text : POST the plain-text body with a ``Title`` header (ntfy.sh topics).
NOTIFY_WEBHOOK_FORMAT = os.environ.get("NOTIFY_WEBHOOK_FORMAT", "json")
NOTIFY_SMTP_HOST = os.environ.get("NOTIFY_SMTP_HOST", "")
NOTIFY_SMTP_PORT = int(os.environ.get("NOTIFY_SMTP_PORT", "587"))
NOTIFY_SMTP_USER = os.environ.get("NOTIFY_SMTP_USER", "")
NOTIFY_SMTP_PASSWORD = os.environ.get("NOTIFY_SMTP_PASSWORD", "")
# STARTTLS after connecting (the norm on port 587). Port 465 always uses
# implicit TLS, whatever this says.
NOTIFY_SMTP_STARTTLS = _env_bool("NOTIFY_SMTP_STARTTLS", default=True)
NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "")

# --- Automatic re-encoding of legal parameters (hacienda_gpt/tax/recodificacion) ---
# When a group of parameters stops matching the text of its law, a model re-reads
# the law and PROPOSES new data; deterministic checks and an independent reviewer
# decide whether the proposal stands. Proposals are JSON files under this directory.
RECODE_PROPOSALS_PATH = os.environ.get("RECODE_PROPOSALS_PATH", "./data/recodificacion")
# true: a proposal that passed every check with nothing left to look at, and that the
# reviewer accepted, is written to the parameter files without waiting for a person
# (the calculator still says "pendiente de revisión humana" until ``params-confirm``).
# false (default): every proposal waits for ``params-apply``.
RECODE_AUTO_APPLY = _env_bool("RECODE_AUTO_APPLY", default=False)
# Reader passes per group before giving up ("sin_converger").
RECODE_MAX_ITER = int(os.environ.get("RECODE_MAX_ITER", "3"))
# Groups re-read in one run (each costs up to 2 LLM calls per pass); the rest wait for the next run.
RECODE_MAX_PER_RUN = int(os.environ.get("RECODE_MAX_PER_RUN", "10"))
# Models of the reader and of the reviewer; empty = the provider's default model.
# A different reviewer model makes the review more independent.
RECODE_MODEL = os.environ.get("RECODE_MODEL", "")
RECODE_REVIEWER_MODEL = os.environ.get("RECODE_REVIEWER_MODEL", "")

# --- Scheduled-job watchdog (hacienda_gpt/ops/vigia.py) ---------------------
# JSON file with the last run / last success / last error of every scheduled job.
JOBS_STATE_PATH = os.environ.get("JOBS_STATE_PATH", "./data/_jobs_state.json")
# Dead-man's-switch URL (healthchecks.io convention) pinged after every watched
# job: the URL itself on success, ``<url>/fail`` on failure. A job-specific
# ``HEALTHCHECK_URL_<JOB_NAME_IN_UPPERCASE>`` (e.g. HEALTHCHECK_URL_BOE_NOVEDADES)
# takes precedence and is read when the job finishes.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

# --- Format drift watch (hacienda_gpt/ops/formatos.py) -----------------------
# Record, on every real read, whether each external format (BOE JSON/XML/HTML,
# crawler listings, user documents) is still understood. Deterministic, no LLM.
FORMAT_WATCH = _env_bool("FORMAT_WATCH", default=True)
# JSON file with the recent observations and the status of every source.
FORMAT_STATE_PATH = os.environ.get("FORMAT_STATE_PATH", "./data/_formatos_state.json")
# Samples (known-good and latest bad, public sources only), applied specs,
# proposals and developer reports.
FORMAT_DIR = os.environ.get("FORMAT_DIR", "./data/formatos")
# Apply a proposed extraction spec without a person when it passed the
# deterministic gate AND the independent reflection had no objection. Off by
# default: proposals wait for `ops formatos apply`.
FORMAT_AUTO_APPLY = _env_bool("FORMAT_AUTO_APPLY", default=False)
# Diagnosis rounds per source before giving up with a report for a developer
# ("sin_converger"; each round costs up to two LLM calls: diagnosis and reflection).
FORMAT_MAX_ITER = int(os.environ.get("FORMAT_MAX_ITER", "3"))
# Models of the diagnosis and of the reflection; empty = the provider's default
# model. A different reviewer model makes the reflection more independent.
FORMAT_MODEL = os.environ.get("FORMAT_MODEL", "")
FORMAT_REVIEWER_MODEL = os.environ.get("FORMAT_REVIEWER_MODEL", "")
# Keep an ANONYMISED skeleton (known labels only; every other word and every
# figure blanked) of user documents whose reading failed, to diagnose reader
# drift. Off by default: nothing derived from a user document is stored.
FORMAT_DOCUMENT_SKELETONS = _env_bool("FORMAT_DOCUMENT_SKELETONS", default=False)

# --- User accounts and privacy (hacienda_gpt/auth) --------------------------
# off      : no accounts; anyone holding a case id can read that case (the
#            historical behaviour — only acceptable on a single-user machine).
# required : every case-scoped endpoint needs a session (Authorization: Bearer)
#            and a case is visible only to the account that created it.
# An unknown value is refused at start-up: a typo must not silently leave the
# cases unprotected.
AUTH_MODE = os.environ.get("AUTH_MODE", "off").strip().lower()
if AUTH_MODE not in {"off", "required"}:
    raise ValueError(f"AUTH_MODE must be 'off' or 'required', not {AUTH_MODE!r}")
# How long a session token is valid after login.
AUTH_SESSION_TTL_HOURS = float(os.environ.get("AUTH_SESSION_TTL_HOURS", "24"))
# scrypt work factor for password hashes (memory is 128 * N * r bytes: 128 MB at
# the default). Stored with every hash, so raising it never locks anyone out.
AUTH_SCRYPT_N = int(os.environ.get("AUTH_SCRYPT_N", str(2**17)))
# Failed logins allowed per e-mail + client address inside the window.
AUTH_LOGIN_MAX_ATTEMPTS = int(os.environ.get("AUTH_LOGIN_MAX_ATTEMPTS", "5"))
AUTH_LOGIN_WINDOW_SECONDS = int(os.environ.get("AUTH_LOGIN_WINDOW_SECONDS", "900"))
# Where the operator publishes the privacy notice shown on the register screen.
# Empty: the app shows its built-in technical summary, which is NOT a legal notice.
PRIVACY_NOTICE_URL = os.environ.get("PRIVACY_NOTICE_URL", "")

# --- LLM spend: measurement and limits (hacienda_gpt/ops/consumo.py) ---------
# All optional; empty = no limit. The USD limits are per calendar day in
# Europe/Madrid and rely on the price table (hacienda_gpt/ops/data/precios_llm.json):
# calls to a model without a known price are recorded with no cost and do not
# count towards them. Read when the agent is built; an invalid value is refused.
LLM_DAILY_BUDGET_USD = os.environ.get("LLM_DAILY_BUDGET_USD", "")
LLM_DAILY_BUDGET_USD_PER_USER = os.environ.get("LLM_DAILY_BUDGET_USD_PER_USER", "")
# Hard cap on model calls spent on ONE user question (agent turns, planner,
# researchers, critic and verifier together).
LLM_MAX_CALLS_PER_QUESTION = os.environ.get("LLM_MAX_CALLS_PER_QUESTION", "")
# Replacement price table (same format as the bundled one), for when prices change.
LLM_PRICES_PATH = os.environ.get("LLM_PRICES_PATH", "")
