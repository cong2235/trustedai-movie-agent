"""Paths and tunable constants in one place so experiments are easy to trace."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


load_dotenv()
DATA_DIR = REPO_ROOT / "data" / "ml-latest-small-filtered"
ARTIFACT_DIR = REPO_ROOT / "artifacts"
OUTPUT_DIR = REPO_ROOT / "outputs"

# --- Ratings ---------------------------------------------------------------
LIKE_THRESHOLD = 4.0          # rating >= this counts as "liked" / relevant
BAYES_PRIOR_COUNT = 10        # pseudo-ratings pulling small-sample means to the global mean

TITLE_MATCH_CONFIDENT = 90   # fuzzy title score above which we accept the top hit

# --- Collaborative filtering -----------------------------------------------
MIN_CO_RATED = 3              # minimum overlap to compute a user-user similarity at all
USER_SIM_SHRINK = 10          # sim *= n / (n + shrink): damp similarities built on few co-ratings
ITEM_SIM_SHRINK = 10
NEIGHBORHOOD_K = 30

# --- Content ---------------------------------------------------------------
# bge-small (local) | openai-3-small | auto ; artifacts live in artifacts/<backend>/
# auto = OpenAI when a key and its index exist (better search: see outputs/eval/search_eval.md), else local bge-small
_embed = os.environ.get("MOVIE_AGENT_EMBEDDINGS", "auto")
if _embed == "auto":
    _embed = ("openai-3-small" if os.environ.get("OPENAI_API_KEY")
              and (ARTIFACT_DIR / "openai-3-small" / "movie_vecs.npy").exists() else "bge-small")
EMBED_BACKEND = _embed
CHUNK_WORDS = 180             # plot chunk size (~240 tokens, well under the 512 limit)
MAX_CHUNKS = 6                # cap per movie; the first chunks carry the premise

# Tags that describe the tagger, not the movie
NOISE_TAGS = {"in netflix queue", "netflix queue", "seen more than once", "own", "dvd", "watched"}

# --- Re-ranking --------------------------------------------------------------
# none | cross (local cross-encoder) | llm (an LLM judges plot excerpts; targets tone/structure queries)
RERANKER = os.environ.get("MOVIE_AGENT_RERANKER", "llm")
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
LLM_RERANK_MODEL = os.environ.get("MOVIE_AGENT_RERANK_MODEL", "gpt-4o-mini")
RERANK_POOL = 30               # candidates sent to the re-ranker
RERANK_TIMEOUT_S = 8.0         # past this, fall back to the stage-1 order (logged as a re-rank error)

# --- Telemetry ---------------------------------------------------------------
LOG_DIR = REPO_ROOT / "logs"
TELEMETRY_DB = LOG_DIR / "telemetry.db"
MEMORY_DB = REPO_ROOT / "data_store" / "memory.db"   # user memories: product data, not logs

# --- Conversation ------------------------------------------------------------
KEEP_FULL_TURNS = 2            # older turns are compacted to (question, final answer) - tool outputs dropped

# --- LLM -------------------------------------------------------------------
DEFAULT_MODEL = "claude-opus-5"
MAX_AGENT_STEPS = 12
# Tail-latency hedging (duplicate an LLM call with no first chunk after N s). OFF by default: measured on the real
# API (80 interleaved calls) it did not help - the ~12 s stalls hit the duplicate too. Set e.g. 2.5 to enable.
HEDGE_AFTER_S = float(os.environ.get("MOVIE_AGENT_HEDGE_AFTER_S", "0"))
