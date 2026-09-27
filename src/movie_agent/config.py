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


def setup_logging(level: str | None = None) -> None:
    """Configure logging for an entry point (library modules only create loggers). MOVIE_AGENT_LOG_LEVEL overrides."""
    import logging

    logging.basicConfig(
        level=(level or os.environ.get("MOVIE_AGENT_LOG_LEVEL", "INFO")).upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


DATA_DIR = REPO_ROOT / "data" / "ml-latest-small-filtered"
ARTIFACT_DIR = REPO_ROOT / "artifacts"
OUTPUT_DIR = REPO_ROOT / "outputs"

LIKE_THRESHOLD = 4.0
BAYES_PRIOR_COUNT = 10

TITLE_MATCH_CONFIDENT = 90

QUALITY_FLOOR_MEAN = 2.75
QUALITY_FLOOR_MIN_COUNT = 3

MIN_CO_RATED = 3
USER_SIM_SHRINK = 10
ITEM_SIM_SHRINK = 10
NEIGHBORHOOD_K = 30

_embed = os.environ.get("MOVIE_AGENT_EMBEDDINGS", "auto")
if _embed == "auto":
    _embed = (
        "openai-3-small"
        if os.environ.get("OPENAI_API_KEY") and (ARTIFACT_DIR / "openai-3-small" / "movie_vecs.npy").exists()
        else "bge-small"
    )
EMBED_BACKEND = _embed
CHUNK_WORDS = 180
MAX_CHUNKS = 6

NOISE_TAGS = {"in netflix queue", "netflix queue", "seen more than once", "own", "dvd", "watched"}

RERANKER = os.environ.get("MOVIE_AGENT_RERANKER", "llm")
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
LLM_RERANK_MODEL = os.environ.get("MOVIE_AGENT_RERANK_MODEL", "gpt-4o-mini")
RERANK_POOL = 30
RERANK_TIMEOUT_S = 8.0

ATTRIBUTE_WEIGHT = 1.0

HTTP_KEEPALIVE_S = float(os.environ.get("MOVIE_AGENT_HTTP_KEEPALIVE_S", "300"))

LOG_DIR = REPO_ROOT / "logs"
TELEMETRY_DB = LOG_DIR / "telemetry.db"
MEMORY_DB = REPO_ROOT / "data_store" / "memory.db"

KEEP_FULL_TURNS = 2

DEFAULT_MODEL = "claude-opus-5"
MAX_AGENT_STEPS = 12
HEDGE_AFTER_S = float(os.environ.get("MOVIE_AGENT_HEDGE_AFTER_S", "0"))
