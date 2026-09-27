"""Hybrid recommender: several interpretable signals, z-scored and linearly blended.

Signals (one score per catalogue movie):
    item_knn   - "people who rated your movies the way you did also rated this highly"
    user_knn   - "your taste-neighbours liked this"
    content    - plot-embedding similarity to the centroid of movies you liked
    quality    - Bayesian-shrunk average rating (guards against 1-rating 5.0 movies)
    popularity - log number of ratings (a mild prior; the eval shows what it costs in novelty)
    pure_svd   - latent-factor score (PureSVD). Not explainable by itself; kept at a small weight because
                 it lifted NDCG@10 in every user segment. Explanations still cite only kNN/content evidence.

A linear blend of z-scores is deliberately simple: each weight is inspectable, the blend is tuned on a
validation split in scripts/evaluate_offline.py, and every recommendation can report which signals
put it on the list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import config
from .cf import CFModel, preference_weight
from .content import ContentIndex
from .data import MovieData

SPARSE_USER_THRESHOLD = 50
PERSONAL_WEIGHT_WITH_REQUEST = 0.3  # share of personal taste when re-ranking a request-retrieved pool
REQUEST_POOL = 40  # candidates retrieved by the explicit request before re-ranking
DEFAULT_WEIGHTS = {"item_knn": 1.0, "user_knn": 0.6, "content": 0.3, "quality": 0.15, "popularity": 0.0}


def _z(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """z-score using statistics of the candidate set only (rated items would skew the scale)."""
    vals = x[mask]
    sd = vals.std()
    return (x - vals.mean()) / (sd if sd > 1e-9 else 1.0)


@dataclass
class Recommender:
    data: MovieData
    cf: CFModel
    content: ContentIndex | None = None
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # optional {"sparse": {...}, "rich": {...}}: different blends by history size (tuned offline)
    segment_weights: dict | None = None

    def weights_for(self, user_id: int) -> dict:
        if self.segment_weights:
            n = len(self.data.user_ratings[user_id])
            return self.segment_weights["sparse" if n < SPARSE_USER_THRESHOLD else "rich"]
        return self.weights

    # ------------------------------------------------------------- signals
    def taste_vector_scores(self, user_id: int) -> np.ndarray:
        """Content similarity to what the user liked (preference-weighted centroid of liked plots)."""
        if self.content is None:
            return np.zeros(len(self.cf.movie_ids))
        ur = self.data.user_ratings[user_id]
        prefs = preference_weight(ur.to_numpy())
        liked = prefs > 0
        if not liked.any():  # nothing above 3 stars: use their relative favourites
            liked = ur.to_numpy() >= ur.median()
            prefs = np.ones_like(prefs)
        rows = np.array([self.cf.m_index[m] for m in ur.index[liked]])
        return self.content.similar_to_movies(rows, prefs[liked])

    def signals(self, user_id: int) -> dict[str, np.ndarray]:
        """Per-user signal vectors, memoised: ratings are static, and a session calls recommend repeatedly."""
        cache = self.__dict__.setdefault("_signal_cache", {})
        if user_id not in cache:
            if len(cache) >= 256:
                cache.pop(next(iter(cache)))
            cache[user_id] = self._compute_signals(user_id)
        return cache[user_id]

    def _compute_signals(self, user_id: int) -> dict[str, np.ndarray]:
        stats = self.data.movie_stats
        return {
            "item_knn": self.cf.item_knn_scores(user_id),
            "user_knn": self.cf.user_knn_scores(user_id),
            "content": self.taste_vector_scores(user_id),
            "quality": stats["bayes"].to_numpy(),
            "popularity": np.log1p(stats["count"].to_numpy()),
            "pure_svd": self.cf.pure_svd_scores(user_id),
        }

    def score(
        self, user_id: int, weights: dict | None = None, signals: dict[str, np.ndarray] | None = None
    ) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
        """Blended score for every movie. Returns (score, z-scored signals, candidate mask)."""
        w = weights or self.weights_for(user_id)
        sig = signals or self.signals(user_id)
        cand = ~self.cf.rated_mask(user_id)
        zs = {k: _z(v, cand) for k, v in sig.items() if w.get(k, 0)}
        total = sum(w[k] * zs[k] for k in zs)
        return total, zs, cand

    # ---------------------------------------------------------------- API
    def recommend(
        self,
        user_id: int,
        n: int = 10,
        *,
        include_genres: list[str] | None = None,
        exclude_genres: list[str] | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
        min_ratings: int = 0,
        min_avg_rating: float | None = None,
        exclude_movie_ids: list[int] | None = None,
        anchor_movie_ids: list[int] | None = None,
        query_relevance: np.ndarray | None = None,
        attribute_relevance: np.ndarray | None = None,
        diversify: bool = True,
        rerank_query: str | None = None,
        reranker=None,
    ) -> list[dict]:
        """Top-n unseen movies for the user under hard constraints, with evidence for each.

        anchor_movie_ids: "more like X" - blends item-item and plot similarity to the anchors into the score.
        query_relevance:  per-movie relevance of a free-text request (from ContentIndex.relevance).
        attribute_relevance: per-movie match with requested moods / twist (from MovieAttributes.match).
        """
        total, zs, cand = self.score(user_id)
        mask = cand & self._constraint_mask(
            include_genres, exclude_genres, min_year, max_year, min_ratings, exclude_movie_ids, min_avg_rating
        )
        if not mask.any():
            return []

        request = np.zeros_like(total)
        if anchor_movie_ids:
            rows = np.array([self.cf.m_index[m] for m in anchor_movie_ids])
            anchor_cf = self.cf.item_sim[:, rows].mean(1)
            anchor_ct = self.content.similar_to_movies(rows) if self.content else np.zeros_like(anchor_cf)
            # Plot embeddings capture topic but not tone (Toy Story -> Child's Play, a killer-doll horror);
            # genre overlap is a coarse tone signal, and co-rating similarity is biased toward popular titles.
            # The three together are more robust than any one of them.
            anchor_gn = self._genre_jaccard(anchor_movie_ids)
            zs["anchor"] = _z(_z(anchor_cf, cand) + _z(anchor_ct, cand) + _z(anchor_gn, cand), cand)
            request += zs["anchor"]
        if query_relevance is not None:
            zs["query"] = _z(query_relevance, cand)
            request += zs["query"]
        if attribute_relevance is not None:
            zs["attributes"] = _z(attribute_relevance, cand)
            request += config.ATTRIBUTE_WEIGHT * zs["attributes"]

        if anchor_movie_ids or query_relevance is not None or attribute_relevance is not None:
            # Two-stage: retrieve by the explicit request, then re-rank that pool by request + personal taste
            # using percentiles. v1 added the request z-score to the personal blend; CF z-scores are heavy-tailed
            # (top items reach z≈10), so "more like Toy Story" returned The Silence of the Lambs.
            idx = np.where(mask)[0]
            pool = idx[np.argsort(-request[idx])[:REQUEST_POOL]]
            pct = lambda x: np.argsort(np.argsort(x)) / max(len(x) - 1, 1)  # noqa: E731
            final = np.full_like(total, -np.inf)
            final[pool] = (1 - PERSONAL_WEIGHT_WITH_REQUEST) * pct(request[pool]) + PERSONAL_WEIGHT_WITH_REQUEST * pct(
                total[pool]
            )
            total = final
            order = list(pool[np.argsort(-total[pool])])
            if rerank_query and reranker is not None:
                # a described mood ("light and funny") is judged by the re-ranker, which reads the plots
                ids = [int(self.cf.movie_ids[i]) for i in order[:30]]
                rr = reranker.rerank(rerank_query, ids, [float(total[self.cf.m_index[m]]) for m in ids], self.data)
                order = [self.cf.m_index[m] for m in rr.order] + order[30:]
                total = total.copy()
                for rank, i in enumerate(order):  # keep MMR consistent with the re-ranked order
                    total[i] = -rank
                zs["rerank_fit_0_10"] = np.full_like(total, np.nan)
                for m, sc in rr.scores.items():
                    zs["rerank_fit_0_10"][self.cf.m_index[m]] = sc
        else:
            order = np.argsort(-np.where(mask, total, -np.inf))[: max(n * 5, 50)]
            order = [i for i in order if mask[i]]
        if diversify:
            order = self._mmr(order, total, n)
        return [self.explain(user_id, int(self.cf.movie_ids[i]), zs=zs, row=i) for i in order[:n]]

    def _genre_jaccard(self, movie_ids: list[int]) -> np.ndarray:
        target = {g for m in movie_ids for g in self.data.movies.loc[m, "genres"]} - {"IMAX"}
        return (
            self.data.movies["genres"]
            .apply(lambda gs: len(target & set(gs)) / max(len(target | (set(gs) - {"IMAX"})), 1))
            .to_numpy(dtype=np.float32)
        )

    def _constraint_mask(
        self, include_genres, exclude_genres, min_year, max_year, min_ratings, exclude_ids, min_avg_rating=None
    ):
        movies = self.data.movies
        mask = np.ones(len(movies), dtype=bool)
        genres = movies["genres"]
        if include_genres:
            inc = {g.lower() for g in include_genres}
            mask &= genres.apply(lambda gs: bool(inc & {g.lower() for g in gs})).to_numpy()
        if exclude_genres:
            exc = {g.lower() for g in exclude_genres}
            mask &= ~genres.apply(lambda gs: bool(exc & {g.lower() for g in gs})).to_numpy()
        if min_year:
            mask &= movies["year"].to_numpy() >= min_year
        if max_year:
            mask &= movies["year"].to_numpy() <= max_year
        if min_ratings:
            mask &= self.data.movie_stats["count"].to_numpy() >= min_ratings
        if min_avg_rating:
            mask &= self.quality_ok(min_avg_rating)
        if exclude_ids:
            for m in exclude_ids:
                if m in self.cf.m_index:
                    mask[self.cf.m_index[m]] = False
        return mask

    def quality_ok(self, min_avg_rating: float) -> np.ndarray:
        """Movies that clear the floor: enough ratings with a mean at or above it, or too few ratings to judge."""
        stats = self.data.movie_stats
        judged = stats["count"].to_numpy() >= config.QUALITY_FLOOR_MIN_COUNT
        return ~judged | (stats["mean"].to_numpy() >= min_avg_rating)

    def _mmr(self, order: list[int], score: np.ndarray, n: int, lam: float = 0.8) -> list[int]:
        """Maximal Marginal Relevance on plot embeddings: avoid 5 sequels of the same franchise."""
        if self.content is None or len(order) <= n:
            return order
        vecs = self.content.movie_vecs * self.content.plot_ok[:, None]  # unreliable plots: no redundancy signal
        s = score[order]
        s = (s - s.min()) / (s.max() - s.min() + 1e-9)
        chosen: list[int] = []
        remaining = list(range(len(order)))
        while remaining and len(chosen) < n:
            if chosen:
                sim = vecs[[order[i] for i in remaining]] @ vecs[[order[c] for c in chosen]].T
                red = sim.max(1)
            else:
                red = np.zeros(len(remaining))
            mmr = lam * s[remaining] - (1 - lam) * red
            best = remaining[int(np.argmax(mmr))]
            chosen.append(best)
            remaining.remove(best)
        return [order[c] for c in chosen]

    # ---------------------------------------------------------- evidence
    def explain(self, user_id: int, movie_id: int, zs: dict | None = None, row: int | None = None) -> dict:
        """Everything the data says about why `user_id` might (or might not) like `movie_id`."""
        data, cf = self.data, self.cf
        row = cf.m_index[movie_id] if row is None else row
        out = data.card(movie_id)
        ur = data.user_ratings[user_id]

        if movie_id in ur.index:
            out["already_rated_by_you"] = float(ur[movie_id])

        # 1. co-rating evidence: which of your ratings drive this (item-item CF)
        out["because_you_rated"] = [
            {"title": data.label(m), "your_rating": r, "co_rating_similarity": round(s, 2)}
            for m, s, r in cf.item_contributions(user_id, movie_id, top=2)
        ]
        # 2. plot evidence: your liked movies with the most similar plots
        plot_ok = data.movies["plot_ok"]
        if self.content is not None and plot_ok[movie_id]:
            liked = [m for m in ur.index if ur[m] >= 4.0 and m != movie_id and plot_ok[m]]
            if liked:
                rows = np.array([cf.m_index[m] for m in liked])
                sims = self.content.movie_vecs[rows] @ self.content.movie_vecs[row]
                top = np.argsort(-sims)[:2]
                out["similar_plots_you_liked"] = [
                    {
                        "title": data.label(liked[i]),
                        "your_rating": float(ur[liked[i]]),
                        "plot_similarity": round(float(sims[i]), 2),
                    }
                    for i in top
                ]
        # 3. people evidence: what your taste-neighbours gave it
        neigh = cf.neighbors_who_rated(user_id, movie_id, k=20)
        if neigh:
            ratings = np.array([n[2] for n in neigh])
            out["similar_users_who_rated_it"] = {
                # counts, not fractions: a fraction field was once misread by the LLM (see REPORT, run 2)
                "n": len(neigh),
                "avg_rating": round(float(ratings.mean()), 2),
                "n_rated_4_or_higher": int((ratings >= 4).sum()),
            }
        pred = cf.predict_rating(user_id, movie_id) if movie_id not in ur.index else {"prediction": None}
        if pred["prediction"] is not None:
            out["predicted_rating_for_you"] = round(pred["prediction"], 1)
        # 4. genre fit
        from .profiles import genre_affinity

        aff = genre_affinity(data, user_id)
        # compact: only genres the user has rated; {"Drama": {"your_avg": 3.86, "n": 35}}
        out["genre_fit"] = {
            g: {"your_avg": round(float(aff.loc[g, "avg_rating"]), 2), "n": int(aff.loc[g, "n_rated"])}
            for g in out["genres"]
            if g in aff.index and aff.loc[g, "n_rated"] > 0
        }
        if zs is not None:
            sig = {k: round(float(v[row]), 1) for k, v in zs.items() if not np.isnan(v[row])}
            out["signal_breakdown_z"] = dict(sorted(sig.items(), key=lambda kv: -kv[1])[:3])  # top 3 drivers
        out["evidence_strength"] = _evidence_strength(out)
        if (fit := _expected_fit(out.get("predicted_rating_for_you"))) is not None:
            out["expected_fit"] = fit
        return out


def _expected_fit(predicted: float | None) -> str | None:
    """What the evidence says, as opposed to how much evidence there is (evidence_strength).

    Found in live use: Kick-Ass had 20 similar raters ("strong" evidence) and a predicted 3.5 stars, and the answer
    presented it like the 4-star picks. The thresholds follow the like threshold (4.0) used everywhere else."""
    if predicted is None:
        return None
    if predicted >= config.LIKE_THRESHOLD:
        return "good match"
    if predicted >= 3.75:
        return "likely match"
    return "uncertain match: say so"


def _evidence_strength(ev: dict) -> str:
    """Coarse confidence label the LLM must surface, so thin evidence is not presented as certainty."""
    n_neigh = ev.get("similar_users_who_rated_it", {}).get("n", 0)
    n_items = len(ev.get("because_you_rated", []))
    if n_neigh >= 5 and n_items >= 2:
        return "strong"
    if n_neigh >= 2 or n_items >= 1:
        return "moderate"
    return "weak (content similarity only)"
