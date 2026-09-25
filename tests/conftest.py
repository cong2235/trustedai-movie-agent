"""Keep the test suite offline and deterministic: no LLM re-ranking, no telemetry writes to logs/."""
import os

os.environ.setdefault("MOVIE_AGENT_RERANKER", "none")
os.environ.setdefault("MOVIE_AGENT_TELEMETRY", "0")
