import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0"))

# --- Embeddings -------------------------------------------------------------
# The app uses a single local, multilingual embedder (Qwen3-Embedding by
# default), built in hacienda_gpt.llm.embeddings and shared by indexing and
# querying so the FAISS vector space always matches. Override the model via
# EMBEDDING_MODEL (any sentence-transformers-compatible repo id).
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-8B")
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "cuda")  # cuda | cpu | mps
EMBEDDING_NORMALIZE = _env_bool("EMBEDDING_NORMALIZE", default=True)
EMBEDDING_DIM = os.environ.get("EMBEDDING_DIM")  # optional MRL truncation, e.g. "1024"

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
