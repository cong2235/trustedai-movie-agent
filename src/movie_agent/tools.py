"""The agent's tool surface: deterministic, JSON-in/JSON-out functions over the dataset.

Design rules
  * Tools return *evidence*, not prose: numbers, titles, counts, so the LLM can reason and cite.
  * Every tool that names a movie accepts a title (fuzzy-matched) or a numeric id, and reports
    ambiguity / absence explicitly instead of guessing ("The Matrix is not in this dataset").
  * Output size is bounded (plots truncated, lists capped) to keep the context window small.
  * The session remembers the current user and what was already suggested, so "something else"
    and "why that one?" work without the LLM re-stating ids.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import numpy as np

from . import config
from .attributes import MOODS, MovieAttributes
from .cf import CFModel
from .content import ContentIndex, _chunks
from .data import MovieData
from .memory import KINDS as MEMORY_KINDS
from .memory import MemoryStore, default_store
from .profiles import blind_spots, user_profile
from .recommender import Recommender, _z
from .rerank import get_reranker


def warm_up(data: MovieData, cf: CFModel, content: ContentIndex | None) -> None:
    """Compute every lazily-built structure at start-up instead of inside the first user's request.

    Measured in the container: the first recommend_movies call took 17 s (item-item matrix, SVD, biases,
    genre shares were all built on demand); warm, the same call takes ~0.1 s.
    """
    from .profiles import population_genre_share

    _ = (data.movie_stats, data.movie_tags, data.user_ratings, data.all_genres)
    data.find_movie("warm-up")  # fuzzy title index
    _ = (cf.user_sim, cf.item_sim, cf.biases)
    cf.pure_svd_scores(int(cf.user_ids[0]))  # fits the SVD factors
    population_genre_share(data)
    if content is not None:
        content.encode_query("warm-up")  # loads the embedder / opens the API connection


_ONE_OFF = re.compile(
    r"\b(tonight|just for|just this|this time|this once|today|right now|at the moment|"
    r"for now|not in the mood)\b",
    re.I,
)
_LASTING = re.compile(
    r"\b(always|never|from now on|in general|any ?more|remember|don't ever|every time|"
    r"generally|usually|hate|can't stand)\b",
    re.I,
)


_RETRACT = re.compile(
    r"(changed my mind|i'?m fine with|i'?m ok(?:ay)? with|no longer|forget (that|it|about)|"
    r"doesn'?t apply|not anymore|i was wrong)",
    re.I,
)


def _is_one_off(message: str) -> bool:
    """'Just for tonight, no comedies' is a request constraint, not a preference to store forever."""
    return bool(message) and bool(_ONE_OFF.search(message)) and not _LASTING.search(message)


class ToolError(Exception):
    """Raised for bad inputs; surfaced to the LLM as an is_error tool_result it can recover from."""

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


@dataclass
class Session:
    user_id: int | None = None
    suggested: list[int] = field(default_factory=list)  # movieIds recommended so far this session
    profile_summary: str = ""  # injected into the model's context each call
    last_user_message: str = ""  # set by the agent each turn (one-off guard)


@dataclass
class MovieTools:
    data: MovieData
    cf: CFModel
    content: ContentIndex | None
    rec: Recommender
    session: Session = field(default_factory=Session)
    trace: list[dict] = field(default_factory=list)
    reranker: object = field(default_factory=get_reranker)
    memory: MemoryStore = field(default_factory=default_store)
    attributes: MovieAttributes | None = None  # offline tone attributes; None when not extracted

    @classmethod
    def build(cls, weights: dict | None = None) -> MovieTools:
        data = MovieData.load()
        cf = CFModel(data)
        try:
            content = ContentIndex.load(data)
        except FileNotFoundError:
            content = None
        rec = Recommender(data, cf, content)
        warm_up(data, cf, content)
        if weights:
            rec.weights = weights
        else:
            tuned = config.OUTPUT_DIR / "eval" / "offline_metrics.json"
            if tuned.exists():
                res = json.loads(tuned.read_text())
                rec.weights = res["tuned_weights"]
                if res.get("use_segment_weights"):
                    rec.segment_weights = res["segment_weights"]
        return cls(data=data, cf=cf, content=content, rec=rec, attributes=MovieAttributes.load(data))

    # ------------------------------------------------------------ helpers
    def _user(self, user_id: int | None) -> int:
        uid = user_id if user_id is not None else self.session.user_id
        if uid is None:
            raise ToolError("No user identified yet. Ask the user for their user ID (1-610).")
        uid = int(uid)
        if not self.data.has_user(uid):
            raise ToolError(f"User {uid} does not exist in the dataset (valid IDs: 1-610).")
        return uid

    def resolve(self, movie: str | int) -> int:
        """Title or id -> movieId, or ToolError listing candidates so the LLM can disambiguate."""
        if isinstance(movie, int) or (isinstance(movie, str) and movie.strip().isdigit()):
            # a digit string may be a title ("21", "1917") - an exact title match wins over the id reading
            if isinstance(movie, str):
                exact = [c for c in self.data.find_movie(movie, limit=3) if c["score"] == 100.0]
                if len(exact) == 1:
                    return exact[0]["movie_id"]
            mid = int(movie)
            if mid in self.cf.m_index:
                return mid
            raise ToolError(f"No movie with id {mid}.")
        cands = self.data.find_movie(str(movie), limit=5)
        if not cands:
            raise ToolError(f"No movie matching '{movie}'.")
        top = cands[0]
        runner_up = cands[1]["score"] if len(cands) > 1 else 0
        if top["score"] >= config.TITLE_MATCH_CONFIDENT and (top["score"] - runner_up >= 1 or top["score"] == 100):
            return top["movie_id"]
        if top["score"] >= 85 and top["score"] - runner_up >= 10:
            return top["movie_id"]
        raise ToolError(
            f"'{movie}' is ambiguous or not in this dataset (5,135 movies, 1903-2014; some famous "
            f"titles are missing). Closest titles below - pick one by movie_id, or tell the user it is absent.",
            closest_titles=cands,
        )

    # -------------------------------------------------------------- tools
    def set_user(self, user_id: int) -> dict:
        uid = self._user(user_id)
        prof = user_profile(self.data, uid, top=5)
        self.session = Session(user_id=uid, profile_summary=self._profile_summary(prof))
        return {
            "ok": True,
            "user_id": uid,
            "n_ratings": prof["n_ratings"],
            "history_size": prof["history_size"],
            "memories": len(self.memory.list(uid)),
        }

    def _profile_summary(self, prof: dict) -> str:
        """Compact profile for the model's context, so most turns need no get_user_profile round trip."""
        top = ", ".join(f"{m['title']} {m['your_rating']:g}★" for m in prof["top_rated"][:5])
        up = ", ".join(f"{g['genre']} {g['avg_rating']}" for g in prof["genres_rated_above_own_average"])
        down = ", ".join(f"{g['genre']} {g['avg_rating']}" for g in prof["genres_rated_below_own_average"])
        return (
            f"Profile: {prof['n_ratings']} ratings, average {prof['avg_rating']}★ ({prof['history_size']} history). "
            f"Top rated: {top}."
            + (f" Rates above own average: {up}." if up else "")
            + (f" Below own average: {down}." if down else "")
        )

    def memory_context(self) -> str:
        uid = self.session.user_id
        return self.memory.summary(uid, self.data.label) if uid is not None else ""

    def _attribute_request(self, moods, twist_ending, max_violence) -> tuple[np.ndarray | None, np.ndarray]:
        """(match score per movie or None, violence mask). Attributes are optional data: without the file, a request
        for them is an error the model can report, not a silent no-op."""
        n = len(self.cf.movie_ids)
        if not (moods or twist_ending or max_violence is not None):
            return None, np.ones(n, dtype=bool)
        if self.attributes is None:
            raise ToolError(
                "Movie attributes are not available (run scripts/extract_attributes.py); "
                "use mood_or_description / query text instead."
            )
        try:
            match = self.attributes.match(moods, bool(twist_ending)) if (moods or twist_ending) else None
        except ValueError as e:
            raise ToolError(str(e)) from e
        return match, self.attributes.violence_ok(None if max_violence is None else int(max_violence))

    def _attribute_card(self, movie_id: int) -> dict | None:
        return self.attributes.card(self.cf.m_index[movie_id]) if self.attributes is not None else None

    def _memory_excluded(self, uid: int) -> list[int]:
        return sorted(self.memory.excluded_movie_ids(uid))

    def _with_avoided_genres(self, uid: int, include_genres, exclude_genres) -> tuple[list[str] | None, list[str]]:
        """Remembered genre dislikes are applied by the tools, not left to the model; an explicit request for
        that genre in the current turn (include_genres) overrides the memory."""
        wanted = {g.lower() for g in include_genres or []}
        from_memory = [g for g in self.memory.avoided_genres(uid) if g.lower() not in wanted]
        # a genre the user explicitly asks for can't also be excluded: the model once sent include=[War] and
        # exclude=[War] together (after reading "War is auto-excluded" in its context), which returned nothing
        merged = [g for g in dict.fromkeys([*(exclude_genres or []), *from_memory]) if g.lower() not in wanted]
        return merged or None, from_memory

    # -------------------------------------------------------- long-term memory
    def remember(
        self, kind: str, movie: str | int | None = None, note: str | None = None, scope: str = "lasting"
    ) -> dict:
        """Store something the user said that should outlive this session.

        scope is the model's own explicit judgement ("lasting" vs "this_request"); the English regex guard below
        stays as a backstop for when the model gets it wrong, not as the primary mechanism."""
        uid = self._user(None)
        if kind not in MEMORY_KINDS:
            raise ToolError(f"kind must be one of {list(MEMORY_KINDS)}")
        if scope not in ("lasting", "this_request"):
            raise ToolError("scope must be 'lasting' or 'this_request'")
        if scope == "this_request":
            raise ToolError(
                "Nothing stored: a constraint for this request only belongs in the tool arguments "
                "(e.g. exclude_genres), not in long-term memory."
            )
        mid = self.resolve(movie) if movie not in (None, "") else None
        if kind in ("avoid_genre", "preference", "dismissed") and _is_one_off(self.session.last_user_message):
            raise ToolError(
                "The user described a constraint for this request only (e.g. 'just for tonight'). "
                "Apply it to this request (e.g. exclude_genres) and do NOT store it."
            )
        if kind in ("avoid_genre", "preference") and _RETRACT.search(self.session.last_user_message or ""):
            # observed: "I'm fine with horror now, forget that" -> the model re-stored avoid_genre=Horror while
            # telling the user it had removed it
            raise ToolError(
                "The user is retracting a stored preference: call forget_memory (e.g. kind='avoid_genre', "
                "genre=...) instead of storing a new memory."
            )
        if kind == "avoid_genre":
            genre = self._genre_in(note)
            if genre is None:
                raise ToolError(f"avoid_genre needs note = one of {self.data.all_genres}")
            note, mid = genre, None
        elif kind != "preference" and mid is None:
            raise ToolError(f"kind '{kind}' needs a movie")
        memory_id = self.memory.add(uid, kind, mid, note)
        effect = (
            "excluded from future recommendations"
            if kind in ("seen", "dismissed", "disliked")
            else f"{note} movies are excluded from future recommendations"
            if kind == "avoid_genre"
            else "kept as context for future sessions"
        )
        return {
            "ok": True,
            "memory_id": memory_id,
            "kind": kind,
            "movie": self.data.label(mid) if mid else None,
            "note": note,
            "effect": effect,
        }

    def _genre_in(self, text: str | None) -> str | None:
        """'Horror', 'horror movies', "I don't like horror movies" -> 'Horror' (exactly one genre named)."""
        if not text:
            return None
        low = text.lower().replace("science fiction", "sci-fi").replace("scifi", "sci-fi")
        hits = [g for g in self.data.all_genres if re.search(rf"\b{re.escape(g.lower())}s?\b", low)]
        return hits[0] if len(hits) == 1 else None

    def forget_memory(
        self,
        memory_id: int | None = None,
        kind: str | None = None,
        genre: str | None = None,
        movie: str | int | None = None,
    ) -> dict:
        """Delete by id, or by content ("forget that I avoid horror": kind='avoid_genre', genre='Horror').
        By-content deletion exists because the model has no memory ids in its context: in 3/3 runs it tried to
        'forget' by storing a new contradicting memory instead."""
        uid = self._user(None)
        mems = self.memory.list(uid)
        if memory_id is not None:
            targets = [m for m in mems if m["memory_id"] == int(memory_id)]
        else:
            g = self._genre_in(genre) if genre else None
            mid = self.resolve(movie) if movie not in (None, "") else None
            targets = [
                m
                for m in mems
                if (kind is None or m["kind"] == kind)
                and (
                    g is None
                    or (m["note"] or "").lower() == g.lower()
                    or (m["kind"] == "preference" and g.lower() in (m["note"] or "").lower())
                )
                and (mid is None or m["movie_id"] == mid)
            ]
            if kind is None and g is None and mid is None:
                raise ToolError("Say what to forget: memory_id, or kind and/or genre and/or movie.")
        removed = []
        for m in targets:
            if self.memory.forget(uid, m["memory_id"]):
                removed.append({"kind": m["kind"], "what": m["note"] or self.data.label(m["movie_id"])})
        return {
            "ok": bool(removed),
            "removed": removed,
            "note": None if removed else "Nothing matched; call list_memories to see what is stored.",
        }

    def list_memories(self) -> dict:
        uid = self._user(None)
        return {
            "user_id": uid,
            "memories": [
                {**m, "movie": self.data.label(m["movie_id"]) if m["movie_id"] else None} for m in self.memory.list(uid)
            ],
        }

    def get_user_profile(self, user_id: int | None = None) -> dict:
        return user_profile(self.data, self._user(user_id))

    def get_rating_history(
        self,
        user_id: int | None = None,
        genre: str | None = None,
        min_rating: float | None = None,
        max_rating: float | None = None,
        title_contains: str | None = None,
        sort: str = "rating_desc",
        limit: int = 20,
    ) -> dict:
        uid = self._user(user_id)
        ur = self.data.user_ratings[uid]
        movies = self.data.movies.loc[ur.index]
        mask = np.ones(len(ur), dtype=bool)
        if genre:
            mask &= movies["genres"].apply(lambda gs: genre.lower() in [g.lower() for g in gs]).to_numpy()
        if min_rating is not None:
            mask &= ur.to_numpy() >= min_rating
        if max_rating is not None:
            mask &= ur.to_numpy() <= max_rating
        if title_contains:
            mask &= movies["display"].str.lower().str.contains(title_contains.lower(), regex=False).to_numpy()
        sel = ur[mask]
        sel = sel.sort_values(ascending=(sort == "rating_asc")) if sort.startswith("rating") else sel
        limit = max(1, min(int(limit), 50))
        return {
            "user_id": uid,
            "n_matching": int(len(sel)),
            "n_total_ratings": int(len(ur)),
            "ratings": [
                {"title": self.data.label(m), "your_rating": float(r), "genres": self.data.movies.loc[m, "genres"]}
                for m, r in sel.head(limit).items()
            ],
        }

    def get_movie_details(self, movie: str | int) -> dict:
        mid = self.resolve(movie)
        out = self.data.card(mid, plot_chars=1200)
        st = self.data.movie_stats.loc[mid]
        if st["count"] >= 2:
            out["rating_std"] = round(float(st["std"]), 2)
        out["catalogue_avg_rating"] = round(self.data.global_mean, 2)
        if self.session.user_id is not None:
            ur = self.data.user_ratings[self.session.user_id]
            out["current_user_rating"] = float(ur[mid]) if mid in ur.index else None
        return out

    def recommend_movies(
        self,
        n: int = 5,
        include_genres: list[str] | None = None,
        exclude_genres: list[str] | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
        min_ratings: int = 3,
        more_like: list[str] | None = None,
        exclude_titles: list[str] | None = None,
        mood_or_description: str | None = None,
        moods: list[str] | None = None,
        twist_ending: bool = False,
        max_violence: int | None = None,
        allow_repeats: bool = False,  # internal only: not in the LLM schema (see TOOL_SCHEMAS)
        user_id: int | None = None,
    ) -> dict:
        uid = self._user(user_id)
        anchors = [self.resolve(t) for t in (more_like or [])]
        # "more like X" must never return X itself (found by the memory test suite: it did whenever the user had
        # not rated X, e.g. "something like The Machinist" -> The Machinist)
        excluded = [self.resolve(t) for t in (exclude_titles or [])] + anchors
        if not allow_repeats:
            excluded += self.session.suggested
        excluded += self._memory_excluded(uid)  # seen / dismissed / disliked in earlier sessions
        exclude_genres, avoided = self._with_avoided_genres(uid, include_genres, exclude_genres)
        rel = self.content.relevance(mood_or_description) if (mood_or_description and self.content) else None
        attr_match, violence_ok = self._attribute_request(moods, twist_ending, max_violence)
        excluded += [int(m) for m in self.cf.movie_ids[~violence_ok]]
        recs = self.rec.recommend(
            uid,
            n=max(1, min(int(n), 10)),
            include_genres=include_genres,
            exclude_genres=exclude_genres,
            min_year=min_year,
            max_year=max_year,
            min_ratings=min_ratings,
            exclude_movie_ids=excluded,
            anchor_movie_ids=anchors or None,
            query_relevance=rel,
            attribute_relevance=attr_match,
            rerank_query=mood_or_description,
            reranker=self.reranker,
        )
        for r in recs:
            if (card := self._attribute_card(r["movie_id"])) is not None:
                r["attributes"] = card
        self.session.suggested += [r["movie_id"] for r in recs]
        return {
            "user_id": uid,
            "applied_constraints": {
                k: v
                for k, v in dict(
                    include_genres=include_genres,
                    exclude_genres=exclude_genres,
                    min_year=min_year,
                    max_year=max_year,
                    min_ratings=min_ratings,
                    more_like=[self.data.label(a) for a in anchors] or None,
                    mood_or_description=mood_or_description,
                    moods=moods,
                    twist_ending=twist_ending or None,
                    max_violence=max_violence,
                    genres_avoided_from_memory=avoided,
                ).items()
                if v
            },
            "excluded_already_suggested": 0 if allow_repeats else len(self.session.suggested) - len(recs),
            "recommendations": recs,
            "note": "signal_breakdown_z = top ranking drivers (item_knn: co-rating with your movies; user_knn: "
            "similar users; content: plot vs your likes; pure_svd: latent factors; anchor/query: your request).",
        }

    def search_movies(
        self,
        query: str,
        n: int = 8,
        include_genres: list[str] | None = None,
        exclude_genres: list[str] | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
        min_ratings: int = 3,
        personalize: bool = True,
        moods: list[str] | None = None,
        twist_ending: bool = False,
        max_violence: int | None = None,
    ) -> dict:
        """Free-text search over plots/tags, re-ranked by quality (and the user's taste if known)."""
        if self.content is None:
            raise ToolError("Content index not built; run scripts/build_index.py.")
        rel = self.content.relevance(query)
        stats = self.data.movie_stats
        all_mask = np.ones(len(rel), dtype=bool)
        quality = _z(stats["bayes"].to_numpy(), all_mask)
        score = rel + 0.35 * quality
        attr_match, violence_ok = self._attribute_request(moods, twist_ending, max_violence)
        if attr_match is not None:
            score = score + config.ATTRIBUTE_WEIGHT * _z(attr_match, all_mask)
        uid = self.session.user_id if personalize else None
        seen = np.zeros(len(rel), dtype=bool)
        if uid is not None:
            exclude_genres, _ = self._with_avoided_genres(uid, include_genres, exclude_genres)
            seen = self.cf.rated_mask(uid)
            for m in self._memory_excluded(uid):
                seen[self.cf.m_index[m]] = True
            taste = _z(self.rec.taste_vector_scores(uid), ~seen)
            score = score + 0.3 * taste
        mask = (
            self.rec._constraint_mask(include_genres, exclude_genres, min_year, max_year, min_ratings, None)
            & ~seen
            & violence_ok
        )
        n = max(1, min(int(n), 15))
        # stage 1: recall-oriented pool; stage 2: re-rank the pool for fit, including tone and structure
        pool = [i for i in np.argsort(-np.where(mask, score, -np.inf))[: max(config.RERANK_POOL, n)] if mask[i]]
        pool_ids = [int(self.cf.movie_ids[i]) for i in pool]
        rr = self.reranker.rerank(query, pool_ids, [float(score[i]) for i in pool], self.data)
        q_vec = self.content.encode_query(query)
        results = []
        for mid in rr.order[:n]:
            i = self.cf.m_index[mid]
            card = self.data.card(mid)
            card["query_match_z"] = round(float(rel[i]), 2)
            if mid in rr.scores:
                card["rerank_fit_0_10"] = rr.scores[mid]
            card["matching_plot_excerpt"] = self._best_chunk(i, q_vec)
            if (attrs := self._attribute_card(mid)) is not None:
                card["attributes"] = attrs
            if uid is not None:  # per-user evidence, so the answer can say why it suits *this* user
                ev = self.rec.explain(uid, mid)
                card["for_you"] = {
                    k: ev[k]
                    for k in (
                        "because_you_rated",
                        "similar_users_who_rated_it",
                        "predicted_rating_for_you",
                        "evidence_strength",
                    )
                    if k in ev
                }
            results.append(card)
        self.session.suggested += [r["movie_id"] for r in results]
        return {
            "query": query,
            "personalized_for_user": uid,
            "excluded_movies_you_rated": bool(uid),
            "reranker": {
                "kind": rr.kind,
                "ms": rr.ms,
                "pool": len(pool_ids),
                **({"error": rr.error} if rr.error else {}),
            },
            "results": results,
            "note": "Stage 1: plot/tag relevance + 0.35*quality z + 0.3*taste z. Stage 2: re-ranker fit score "
            "(0-10, judges tone/structure from title, tags and plot excerpt) blended 0.7/0.3 with stage 1. "
            "Confirm claims like 'has a twist' from the excerpt or tags.",
        }

    def _best_chunk(self, row: int, q_vec: np.ndarray, chars: int = 350) -> str:
        if not self.content.plot_ok[row]:
            return "[no reliable plot for this title - matched on title/genres/tags only]"
        idx = np.where(self.content.chunk_owner == row)[0]
        best = int(np.argmax(self.content.chunk_vecs[idx] @ q_vec))
        text = _chunks(self.data.movies.iloc[row]["plot"])[best]
        return text[:chars].rsplit(" ", 1)[0] + " ..."

    def find_similar_users(self, k: int = 10, user_id: int | None = None) -> dict:
        uid = self._user(user_id)
        ur = self.data.user_ratings[uid]
        out = []
        for v, sim, n in self.cf.similar_users(uid, k=max(1, min(int(k), 20))):
            vr = self.data.user_ratings[v]
            common = ur.index.intersection(vr.index)
            both_love = [m for m in common if ur[m] >= 4.5 and vr[m] >= 4.5]
            both_love.sort(key=lambda m: -self.data.movie_stats.loc[m, "count"])
            out.append(
                {
                    "user_id": v,
                    "similarity": round(sim, 3),
                    "n_movies_in_common": n,
                    "n_ratings": int(len(vr)),
                    "mean_abs_rating_gap_on_common": round(float((ur[common] - vr[common]).abs().mean()), 2),
                    "both_loved": [self.data.label(m) for m in both_love[:4]],
                }
            )
        return {
            "user_id": uid,
            "method": "Pearson correlation on co-rated movies x n/(n+10) overlap shrinkage",
            "similar_users": out,
        }

    def similar_users_opinion(self, movie: str | int, k: int = 20, user_id: int | None = None) -> dict:
        """What the k users most similar to you (among those who rated the movie) think of it."""
        uid = self._user(user_id)
        mid = self.resolve(movie)
        neigh = self.cf.neighbors_who_rated(uid, mid, k=max(10, min(int(k), 50)))  # <10 is too noisy to summarise
        st = self.data.movie_stats.loc[mid]
        ur = self.data.user_ratings[uid]
        out = {
            "movie": self.data.label(mid),
            "movie_id": mid,
            "your_rating": float(ur[mid]) if mid in ur.index else None,
            "everyone": {
                "n": int(st["count"]),
                "avg_rating": None if st["count"] == 0 else round(float(st["mean"]), 2),
            },
        }
        if not neigh:
            out["similar_users"] = {
                "n": 0,
                "note": "None of the users who rated this movie has a positive taste "
                "similarity with you - no collaborative evidence.",
            }
            return out
        sims = np.array([n[1] for n in neigh])
        ratings = np.array([n[2] for n in neigh])
        out["similar_users"] = {
            "n": len(neigh),
            "weighted_avg_rating": round(float((sims * ratings).sum() / sims.sum()), 2),
            "plain_avg_rating": round(float(ratings.mean()), 2),
            # counts, not fractions: gpt-4o-mini read "share_2_5_or_less": 0.1 as "nobody rated it below 2.5"
            "n_rated_4_or_higher": int((ratings >= 4).sum()),
            "n_rated_2_5_or_lower": int((ratings <= 2.5).sum()),
            "similarity_range": [round(float(sims.min()), 2), round(float(sims.max()), 2)],
            "individual": [
                {"user_id": v, "similarity": round(s, 2), "their_rating": r, "movies_in_common_with_you": n}
                for v, s, r, n in neigh[:8]
            ],
        }
        if out["your_rating"] is None:
            pred = self.cf.predict_rating(uid, mid)
            if pred["prediction"] is not None:
                out["predicted_rating_for_you"] = round(pred["prediction"], 1)
        else:
            out["note"] = "You already rated this movie, so no prediction is made; compare your rating with theirs."
        out["reliability"] = (
            "high"
            if len(neigh) >= 10 and sims.mean() > 0.2
            else "medium"
            if len(neigh) >= 4
            else "low - very few similar users rated it"
        )
        return out

    def explain_match(self, movie: str | int, user_id: int | None = None) -> dict:
        """Evidence for and against the user liking a movie."""
        uid = self._user(user_id)
        mid = self.resolve(movie)
        ev = self.rec.explain(uid, mid)
        # counter-evidence: plot-similar movies the user rated low
        ur = self.data.user_ratings[uid]
        plot_ok = self.data.movies["plot_ok"]
        disliked = [m for m in ur.index if ur[m] <= 2.5 and m != mid and plot_ok[m]]
        if disliked and self.content is not None and plot_ok[mid]:
            rows = np.array([self.cf.m_index[m] for m in disliked])
            sims = self.content.movie_vecs[rows] @ self.content.movie_vecs[self.cf.m_index[mid]]
            top = np.argsort(-sims)[:2]
            ev["similar_plots_you_disliked"] = [
                {
                    "title": self.data.label(disliked[i]),
                    "your_rating": float(ur[disliked[i]]),
                    "plot_similarity": round(float(sims[i]), 2),
                }
                for i in top
                if sims[i] > 0.6
            ]
        return ev

    def genre_blind_spots(self, user_id: int | None = None) -> dict:
        return blind_spots(self.data, self.cf, self._user(user_id))

    # ------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool by name. Returns (json_text, is_error) and records a trace entry."""
        t0 = time.time()
        fn = getattr(self, name, None) if name in TOOL_NAMES else None
        try:
            if fn is None:
                raise ToolError(f"Unknown tool {name}")
            result, is_error = fn(**_coerce_args(name, args)), False
        except ToolError as e:
            result, is_error = {"error": str(e), **e.details}, True
        except TypeError as e:  # bad/missing arguments from the model
            result, is_error = {"error": f"Invalid arguments: {e}"}, True
        except Exception as e:  # an unexpected tool bug must not kill the whole turn; the model can recover
            result, is_error = {"error": f"Tool failed: {type(e).__name__}: {e}"}, True
        text = json.dumps(result, default=_json_default, ensure_ascii=False)
        self.trace.append(
            {
                "tool": name,
                "input": args,
                "is_error": is_error,
                "ms": round((time.time() - t0) * 1000),
                "output_chars": len(text),
                "output": result,
            }
        )
        return text, is_error


def _coerce_args(name: str, args: dict) -> dict:
    """Make model-supplied arguments match the schema types before the tool sees them.

    Found by the memory suite: gpt-4o-mini sent more_like="The Machinist (2004)" (a string, not a list); the tool
    iterated over its characters and recommended The Fugitive, Batman, The Mask... The same would silently break
    include_genres="War". Non-strict function calling does not enforce types, so the tools do.
    """
    schema = next((t["input_schema"]["properties"] for t in TOOL_SCHEMAS if t["name"] == name), {})
    out = dict(args)
    for key, value in args.items():
        typ = schema.get(key, {}).get("type")
        if typ == "array" and isinstance(value, str):
            out[key] = [value]
        elif typ == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            out[key] = int(value)
        elif typ == "integer" and isinstance(value, float) and value.is_integer():
            out[key] = int(value)
        elif typ == "boolean" and isinstance(value, str):
            out[key] = value.strip().lower() in ("true", "1", "yes")
    return out


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    raise TypeError(type(o))


# ------------------------------------------------------------------ schemas
_GENRES = (
    "Action, Adventure, Animation, Children, Comedy, Crime, Documentary, Drama, Fantasy, Film-Noir, "
    "Horror, IMAX, Musical, Mystery, Romance, Sci-Fi, Thriller, War, Western"
)
_movie = {
    "type": "string",
    "description": "Movie title (fuzzy matched; add the year to disambiguate remakes) or numeric movie_id.",
}
_attribute_args = {
    "moods": {
        "type": "array",
        "items": {"type": "string", "enum": list(MOODS)},
        "description": "Tone the user asked for, matched against pre-computed movie attributes (plot text alone "
        "misses tone). 'light and funny' -> ['light-hearted', 'funny']; 'dark comedy' -> "
        "['dark-comedy']. Keep the words in the free-text query as well.",
    },
    "twist_ending": {"type": "boolean", "description": "true when the user wants a twist / surprise ending."},
    "max_violence": {
        "type": "integer",
        "description": "0-3. 'nothing violent' -> 1, 'not too violent' -> 2. Movies above this level are removed.",
    },
}
_genre_list = {"type": "array", "items": {"type": "string"}, "description": f"Genres from: {_GENRES}."}
_user = {"type": "integer", "description": "Defaults to the current session user; only set to inspect someone else."}

TOOL_SCHEMAS = [
    {
        "name": "get_user_profile",
        "description": "Summarise a user's rating history: size, generosity, top/bottom rated movies, most-watched genres "
        "(with lift vs. population) and genres they rate above/below their own average. Call this first "
        "for any personalised request, and to ground explanations.",
        "input_schema": {"type": "object", "properties": {"user_id": _user}},
    },
    {
        "name": "get_rating_history",
        "description": "List the user's own ratings, filterable by genre, rating range or title substring. Use to check "
        "whether they have seen something, or to find patterns (e.g. all their Horror ratings).",
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {"type": "string"},
                "min_rating": {"type": "number"},
                "max_rating": {"type": "number"},
                "title_contains": {"type": "string"},
                "sort": {"type": "string", "enum": ["rating_desc", "rating_asc"]},
                "limit": {"type": "integer", "description": "max 50"},
                "user_id": _user,
            },
        },
    },
    {
        "name": "get_movie_details",
        "description": "Plot (truncated), genres, rating count/avg/std and top tags for one movie; also the current user's "
        "rating if they rated it. Reports clearly when a title is not in the dataset.",
        "input_schema": {"type": "object", "properties": {"movie": _movie}, "required": ["movie"]},
    },
    {
        "name": "recommend_movies",
        "description": "Personalised recommendations for the current user from a hybrid of collaborative filtering "
        "(item-item and user-user), plot similarity to their liked movies and a quality prior. Supports "
        "hard constraints (genres, years, min #ratings), 'more like these titles', and a free-text mood. "
        "Excludes movies the user rated and movies already suggested this session. Each result includes "
        "evidence: which of their ratings drive it, similar users' ratings, genre fit, evidence_strength.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "1-10, default 5"},
                "include_genres": {
                    **_genre_list,
                    "description": "Keep only movies having at least one of these genres.",
                },
                "exclude_genres": {**_genre_list, "description": "Drop movies having any of these genres."},
                "min_year": {
                    "type": "integer",
                    "description": "Inclusive. 'after 2000' -> 2001; 'from 2000 on' -> 2000.",
                },
                "max_year": {
                    "type": "integer",
                    "description": "Inclusive. 'before 1970' -> 1969; 'up to 1970' -> 1970.",
                },
                "min_ratings": {
                    "type": "integer",
                    "description": "Minimum number of ratings in the dataset (quality/reliability floor). Default 3.",
                },
                "more_like": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference titles. Set this WHENEVER the user names a movie they liked or wants "
                    "'something like X' - it is the only way results will resemble X (genre filters "
                    "alone just return the user's generic favourites).",
                },
                "exclude_titles": {"type": "array", "items": {"type": "string"}},
                "mood_or_description": {
                    "type": "string",
                    "description": "Free-text vibe, e.g. 'feel-good heist comedy'.",
                },
                **_attribute_args,
                "user_id": _user,
            },
        },
    },
    {
        "name": "search_movies",
        "description": "Search the catalogue by description/theme over full plot summaries and user tags (semantic + "
        "keyword), re-ranked by rating quality and, if a user is set, their taste. Returns the plot "
        "excerpt that matched. Use for 'a dark psychological thriller with a twist'-style requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "n": {"type": "integer", "description": "1-15, default 8"},
                "include_genres": _genre_list,
                "exclude_genres": _genre_list,
                "min_year": {
                    "type": "integer",
                    "description": "Inclusive. 'after 2000' -> 2001; 'from 2000 on' -> 2000.",
                },
                "max_year": {
                    "type": "integer",
                    "description": "Inclusive. 'before 1970' -> 1969; 'up to 1970' -> 1970.",
                },
                "min_ratings": {"type": "integer", "description": "Default 3."},
                "personalize": {
                    "type": "boolean",
                    "description": "Blend in the user's taste and hide movies they rated. Default true.",
                },
                **_attribute_args,
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_similar_users",
        "description": "The users whose ratings correlate most with the current user's, with overlap size, average rating "
        "gap and movies both loved. Use to explain 'people like you'.",
        "input_schema": {
            "type": "object",
            "properties": {"k": {"type": "integer", "description": "max 20"}, "user_id": _user},
        },
    },
    {
        "name": "similar_users_opinion",
        "description": "What users with similar taste think of a specific movie: among users who rated it, the most "
        "similar to the current user, their weighted/plain average, rating split, and a predicted rating for "
        "the current user, compared with everyone's average. Includes a reliability label.",
        "input_schema": {
            "type": "object",
            "properties": {"movie": _movie, "k": {"type": "integer"}, "user_id": _user},
            "required": ["movie"],
        },
    },
    {
        "name": "explain_match",
        "description": "Evidence for and against the current user liking a movie: their ratings of co-rated similar movies, "
        "their liked movies with similar plots, plot-similar movies they disliked, similar users' ratings, "
        "predicted rating, genre fit. Use for 'why would I like that?'.",
        "input_schema": {"type": "object", "properties": {"movie": _movie, "user_id": _user}, "required": ["movie"]},
    },
    {
        "name": "genre_blind_spots",
        "description": "Genres the user watches much less than the average user, ranked by how much their taste-neighbours "
        "enjoy them, with concrete entry-point movies those neighbours rated highly.",
        "input_schema": {"type": "object", "properties": {"user_id": _user}},
    },
]
TOOL_SCHEMAS += [
    {
        "name": "remember",
        "description": "Save something the user said that should persist across sessions. Call it when the user says "
        "they have seen a movie ('seen'), are not interested in one ('dismissed'), watched and disliked "
        "or liked one ('disliked' / 'liked'), doesn't want a genre ('avoid_genre' + note = the genre, "
        "e.g. 'War'), or states another lasting preference ('preference' + note). seen/dismissed/"
        "disliked movies and avoided genres are then excluded automatically in later sessions. "
        "Do not store one-off constraints for the current request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                "movie": _movie,
                "note": {"type": "string", "description": "for 'preference', the preference in a few words"},
                "scope": {
                    "type": "string",
                    "enum": ["lasting", "this_request"],
                    "description": '\'lasting\' only if the user means it beyond this request ("never", "in general", '
                    '"remember that", or a fact such as having seen a movie). A mood or constraint '
                    'for now ("tonight", "this time") is \'this_request\': pass it as tool arguments '
                    "instead and do not call remember at all.",
                },
            },
            "required": ["kind", "scope"],
        },
    },
    {
        "name": "list_memories",
        "description": "List what is remembered about the current user from earlier sessions (with memory ids).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "forget_memory",
        "description": "Delete remembered items when the user retracts something ('I'm fine with horror now', 'I haven't "
        "actually seen Big'). Delete by content - e.g. kind='avoid_genre' + genre='Horror', or kind='seen' + "
        "movie - or by memory_id from list_memories. Never 'forget' by storing a new contradicting memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                "genre": {"type": "string"},
                "movie": _movie,
            },
        },
    },
]
TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS} | {"set_user"}
