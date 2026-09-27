"""Second-stage re-rankers for free-text requests.

The first stage (embeddings + TF-IDF + quality) is recall-oriented and matches what a plot is *about*.
It failed on tone and structure ("light and funny" -> dramas about comedians; "twist ending" -> P@10 = 0).
A re-ranker looks at each (request, candidate) pair jointly:

    cross   ms-marco MiniLM cross-encoder, local, ~0.3 s for 30 candidates. Better relevance, same blind spot for tone.
    llm     one listwise LLM call: the model reads title, genres, tags and a plot excerpt for ~30 candidates
            and scores fit 0-10, *including tone and structure*. ~2-4 s, ~$0.001 per request with gpt-4o-mini.

Final order = blend of the re-ranker score and the first-stage score (both z-scored within the pool), so a
re-ranker glitch cannot throw away a strong first-stage match. Every call is timed and logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

import numpy as np

from . import config
from .content import _chunks
from .data import MovieData


def candidate_text(data: MovieData, movie_id: int, plot_chars: int = 600, show_tags: bool = True) -> str:
    """show_tags=False in the search eval, where tags are the relevance labels."""
    row = data.movies.loc[movie_id]
    tags = ", ".join(data.movie_tags.get(movie_id, [])[:8]) if show_tags else ""
    plot = row["plot"][:plot_chars] if row["plot_ok"] else "(plot unavailable)"
    return (
        f"{row['display']} ({row['year']}) | genres: {', '.join(row['genres'])}"
        + (f" | tags: {tags}" if tags else "")
        + f" | plot: {plot}"
    )


def _z(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-9)


@dataclass
class RerankResult:
    order: list[int]
    scores: dict[int, float]
    ms: int
    kind: str
    error: str | None = None


class NoRerank:
    kind = "none"

    def rerank(self, query, movie_ids, first_stage, data) -> RerankResult:
        order = [m for _, m in sorted(zip(-np.asarray(first_stage), movie_ids))]
        return RerankResult(order=order, scores={}, ms=0, kind=self.kind)


class _Blend:
    blend = 0.7
    show_tags = True

    def _finish(self, movie_ids, first_stage, raw: dict[int, float], t0: float, error=None) -> RerankResult:
        fs = _z(np.asarray(first_stage, dtype=float))
        rr = np.array([raw.get(m, np.nan) for m in movie_ids], dtype=float)
        rr = np.where(np.isnan(rr), np.nanmin(rr) if np.isfinite(rr).any() else 0.0, rr)
        final = self.blend * _z(rr) + (1 - self.blend) * fs
        order = [movie_ids[i] for i in np.argsort(-final)]
        return RerankResult(order=order, scores=raw, ms=round((time.time() - t0) * 1000), kind=self.kind, error=error)


@dataclass
class CrossEncoderRerank(_Blend):
    kind: str = "cross"
    _model: object = None

    def rerank(self, query, movie_ids, first_stage, data) -> RerankResult:
        t0 = time.time()
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(config.CROSS_ENCODER_MODEL)
        pairs = []
        for m in movie_ids:
            row = data.movies.loc[m]
            plot = _chunks(row["plot"])[0] if row["plot_ok"] else ""
            pairs.append((query, candidate_text(data, m, 0, self.show_tags) + " " + plot))
        scores = self._model.predict(pairs, show_progress_bar=False)
        return self._finish(movie_ids, first_stage, {m: float(s) for m, s in zip(movie_ids, scores)}, t0)


LLM_RERANK_PROMPT = """You rank movies for a request. For each candidate, score 0-10 how well it fits the request, judging from the title, genres, tags and plot excerpt. Consider tone and structure, not only topic: a drama *about* comedians is not a "light and funny" movie; a plain crime story without a reveal does not have "a twist". Use general film knowledge only to interpret the excerpt, not to invent facts.
Return JSON: {"scores": [[<candidate id>, <0-10>], ...]} covering every candidate."""


@dataclass
class LLMRerank(_Blend):
    """Listwise LLM re-ranker. Output format was measured (30 queries, tags hidden, gpt-4o-mini):
    {"id":.., "score":..} objects, plot 600 chars   NDCG topic 0.422 / tone 0.117, 3.8 s
    bare score array in candidate order             0.294 / 0.067, 1.8 s  (the model loses its place)
    [id, score] pairs, plot 400 chars  (shipped)    0.429 / 0.124, 2.3 s"""

    kind: str = "llm"
    model: str = config.LLM_RERANK_MODEL
    plot_chars: int = 400
    cache_path: object = config.LOG_DIR / "rerank_cache.json"
    _cache: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.cache_path.exists():
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def rerank(self, query, movie_ids, first_stage, data) -> RerankResult:
        t0 = time.time()
        key = hashlib.sha1(
            f"v3|{self.model}|{self.show_tags}|{self.plot_chars}|{query}|{movie_ids}".encode()
        ).hexdigest()
        if key in self._cache:
            raw = {int(k): v for k, v in self._cache[key].items()}
            res = self._finish(movie_ids, first_stage, raw, t0)
            res.kind = "llm-cache"
            return res
        listing = "\n".join(
            f"[{i}] {candidate_text(data, m, self.plot_chars, self.show_tags)}" for i, m in enumerate(movie_ids)
        )
        try:
            resp = _openai_client().chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=500,
                messages=[
                    {"role": "system", "content": LLM_RERANK_PROMPT},
                    {"role": "user", "content": f"Request: {query}\n\n{len(movie_ids)} candidates:\n{listing}"},
                ],
            )
            pairs = json.loads(resp.choices[0].message.content)["scores"]
            raw = {
                movie_ids[int(p[0])]: float(p[1])
                for p in pairs
                if isinstance(p, list) and len(p) == 2 and str(p[0]).isdigit() and int(p[0]) < len(movie_ids)
            }
            error = None if len(raw) == len(movie_ids) else f"scored {len(raw)} of {len(movie_ids)} candidates"
        except Exception as e:
            return self._finish(movie_ids, first_stage, {}, t0, error=f"{type(e).__name__}: {e}"[:200])
        self._cache[key] = raw
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
        return self._finish(movie_ids, first_stage, raw, t0, error=error)


_CLIENT = {}


def _openai_client():
    """One shared client (keeps the TLS connection warm). A hard timeout bounds the worst case: API latency
    spikes of 19-27 s were observed in testing; past 8 s the request falls back to the stage-1 order."""
    if "c" not in _CLIENT:
        from .http import openai_client

        _CLIENT["c"] = openai_client(timeout=config.RERANK_TIMEOUT_S, max_retries=1)
    return _CLIENT["c"]


def get_reranker(kind: str | None = None):
    kind = kind or config.RERANKER
    if kind == "llm" and not os.environ.get("OPENAI_API_KEY"):
        kind = "cross"
    return {"none": NoRerank, "cross": CrossEncoderRerank, "llm": LLMRerank}[kind]()
