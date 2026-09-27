"""Collaborative filtering: user-user and item-item neighbourhoods on a sparse rating matrix.

Neighbourhood methods were chosen over latent-factor models as the *serving* model because
every score decomposes into named people or named movies ("7 users with your taste gave it
4.4", "because you rated Aliens 5.0"), which is what a conversational explainer needs.
PureSVD is kept here as an evaluation baseline to show what that choice costs in accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np
import scipy.sparse as sp

from . import config
from .data import MovieData


def preference_weight(ratings: np.ndarray) -> np.ndarray:
    """Map a 0.5–5 star rating to a signed preference in [-1, 1]: 5 -> 1, 4 -> .5, 3 -> 0, 1 -> -1.

    Absolute rather than user-mean-centred: user 30 averages 4.6, and mean-centring would turn
    their 4-star ratings into "dislikes". Centring is still used for *similarity* (Pearson),
    where it removes rater generosity.
    """
    return np.clip((ratings - 3.0) / 2.0, -1.0, 1.0)


@dataclass
class CFModel:
    data: MovieData

    @cached_property
    def user_ids(self) -> np.ndarray:
        return np.sort(self.data.ratings["userId"].unique())

    @cached_property
    def movie_ids(self) -> np.ndarray:
        return self.data.movies.index.to_numpy()

    @cached_property
    def u_index(self) -> dict[int, int]:
        return {int(u): i for i, u in enumerate(self.user_ids)}

    @cached_property
    def m_index(self) -> dict[int, int]:
        return {int(m): i for i, m in enumerate(self.movie_ids)}

    @cached_property
    def R(self) -> sp.csr_matrix:
        """Raw ratings (n_users, n_movies); 0 = unrated."""
        r = self.data.ratings
        rows = r["userId"].map(self.u_index).to_numpy()
        cols = r["movieId"].map(self.m_index).to_numpy()
        return sp.csr_matrix(
            (r["rating"].to_numpy(np.float32), (rows, cols)), shape=(len(self.user_ids), len(self.movie_ids))
        )

    @cached_property
    def B(self) -> sp.csr_matrix:
        """Binary 'has rated' mask."""
        b = self.R.copy()
        b.data[:] = 1.0
        return b

    @cached_property
    def user_mean(self) -> np.ndarray:
        return np.asarray(self.R.sum(1)).ravel() / np.maximum(np.asarray(self.B.sum(1)).ravel(), 1)

    @cached_property
    def Rc(self) -> sp.csr_matrix:
        """Ratings centred on each user's mean (stored entries only)."""
        rc = self.R.copy().tocoo()
        rc.data = rc.data - self.user_mean[rc.row]
        return rc.tocsr()

    @cached_property
    def W(self) -> sp.csr_matrix:
        """Signed preference weights (see preference_weight)."""
        w = self.R.copy()
        w.data = preference_weight(w.data)
        return w

    @cached_property
    def user_sim(self) -> np.ndarray:
        """Pearson correlation on co-rated items, significance-weighted by overlap.

        sim(u,v) = pearson(u,v) * n/(n+shrink), zeroed when n < MIN_CO_RATED.
        Plain cosine over the full vectors would reward users for simply rating many of the
        same popular movies; restricting norms to the co-rated set avoids that.
        """
        Rc, B = self.Rc, self.B
        num = (Rc @ Rc.T).toarray()
        sq = Rc.multiply(Rc)
        norm_uv = (sq @ B.T).toarray()
        denom = np.sqrt(norm_uv * norm_uv.T) + 1e-9
        n = (B @ B.T).toarray()
        sim = num / denom
        sim *= n / (n + config.USER_SIM_SHRINK)
        sim[n < config.MIN_CO_RATED] = 0.0
        np.fill_diagonal(sim, 0.0)
        self._co_counts = n
        return sim.astype(np.float32)

    @cached_property
    def co_counts(self) -> np.ndarray:
        _ = self.user_sim
        return self._co_counts

    def similar_users(self, user_id: int, k: int = config.NEIGHBORHOOD_K) -> list[tuple[int, float, int]]:
        """[(userId, similarity, n_co_rated)] of the k most similar users with positive similarity."""
        u = self.u_index[user_id]
        sims = self.user_sim[u]
        top = np.argsort(-sims)[:k]
        return [(int(self.user_ids[v]), float(sims[v]), int(self.co_counts[u, v])) for v in top if sims[v] > 0]

    def neighbors_who_rated(self, user_id: int, movie_id: int, k: int = 20) -> list[tuple[int, float, float, int]]:
        """Item-specific neighbourhood: the k most similar users *among those who rated movie_id*.

        Returns [(userId, similarity, their rating, n_co_rated)].
        """
        u, m = self.u_index[user_id], self.m_index[movie_id]
        raters = self.R[:, m].nonzero()[0]
        raters = raters[raters != u]
        sims = self.user_sim[u, raters]
        order = np.argsort(-sims)[:k]
        return [
            (
                int(self.user_ids[raters[i]]),
                float(sims[i]),
                float(self.R[raters[i], m]),
                int(self.co_counts[u, raters[i]]),
            )
            for i in order
            if sims[i] > 0
        ]

    @cached_property
    def biases(self) -> tuple[float, np.ndarray, np.ndarray]:
        """Regularised baseline r_ui ≈ mu + b_u + b_i (Koren 2008), fitted by two closed-form passes."""
        R, B = self.R.tocoo(), self.B
        mu = float(R.data.mean())
        n_i = np.asarray(B.sum(0)).ravel()
        b_i = np.bincount(R.col, weights=R.data - mu, minlength=R.shape[1]) / (n_i + 10)
        n_u = np.asarray(B.sum(1)).ravel()
        b_u = np.bincount(R.row, weights=R.data - mu - b_i[R.col], minlength=R.shape[0]) / (n_u + 5)
        return mu, b_u, b_i

    def baseline(self, u: int, m: int) -> float:
        mu, b_u, b_i = self.biases
        return mu + b_u[u] + b_i[m]

    def predict_rating(
        self, user_id: int, movie_id: int, k: int = 20, shrink: float = 1.0, method: str = "residual"
    ) -> dict:
        """kNN rating prediction with its evidence size.

        method="residual" (default): baseline + similarity-weighted neighbour residuals, shrunk toward the
        baseline when few/weak neighbours exist. The first version ("mean_centered", classic Resnick) was
        *worse than the bias baseline* in offline eval with 1-2 neighbours - see outputs/eval.
        """
        u, m = self.u_index[user_id], self.m_index[movie_id]
        neigh = self.neighbors_who_rated(user_id, movie_id, k)
        if not neigh:
            return {"prediction": None, "n_neighbors": 0, "baseline": float(np.clip(self.baseline(u, m), 0.5, 5))}
        s = np.array([n[1] for n in neigh])
        vs = [self.u_index[n[0]] for n in neigh]
        if method == "mean_centered":
            dev = np.array([n[2] - self.user_mean[v] for n, v in zip(neigh, vs)])
            pred = self.user_mean[u] + float((s * dev).sum() / (np.abs(s).sum() + 1e-9))
        else:
            dev = np.array([n[2] - self.baseline(v, m) for n, v in zip(neigh, vs)])
            pred = self.baseline(u, m) + float((s * dev).sum() / (np.abs(s).sum() + shrink))
        return {
            "prediction": float(np.clip(pred, 0.5, 5.0)),
            "n_neighbors": len(neigh),
            "baseline": float(np.clip(self.baseline(u, m), 0.5, 5)),
        }

    def user_knn_scores(self, user_id: int, k: int = config.NEIGHBORHOOD_K) -> np.ndarray:
        """Top-N ranking score for every movie: sum over the user's k neighbours of sim * preference.

        A sum (not an average) so that items endorsed by several neighbours beat items
        one neighbour happened to love.
        """
        u = self.u_index[user_id]
        sims = self.user_sim[u]
        top = np.argsort(-sims)[:k]
        top = top[sims[top] > 0]
        return np.asarray(sims[top] @ self.W[top].toarray()).ravel()

    @cached_property
    def item_sim(self) -> np.ndarray:
        """Adjusted cosine between movies (user-mean-centred), significance-weighted by co-raters."""
        Rc = self.Rc.tocsc()
        num = (Rc.T @ Rc).toarray()
        norms = np.sqrt(np.asarray(Rc.multiply(Rc).sum(0)).ravel()) + 1e-9
        sim = num / np.outer(norms, norms)
        n = (self.B.T @ self.B).toarray()
        sim *= n / (n + config.ITEM_SIM_SHRINK)
        np.fill_diagonal(sim, 0.0)
        return sim.astype(np.float32)

    def item_knn_scores(self, user_id: int, k: int = config.NEIGHBORHOOD_K) -> np.ndarray:
        """For each movie: sum of sim(movie, j) * preference(j) over its k most similar movies the user rated."""
        u = self.u_index[user_id]
        rated = self.R[u].indices
        prefs = self.W[u].data
        S = self.item_sim[:, rated]
        if len(rated) > k:
            cut = np.argpartition(-S, k - 1, axis=1)[:, :k]
            mask = np.zeros_like(S, dtype=bool)
            np.put_along_axis(mask, cut, True, axis=1)
            S = np.where(mask, S, 0.0)
        S = np.clip(S, 0.0, None)
        return S @ prefs

    def item_contributions(self, user_id: int, movie_id: int, top: int = 5) -> list[tuple[int, float, float]]:
        """Which of the user's rated movies push movie_id up: [(movieId, sim, user's rating)]."""
        u, m = self.u_index[user_id], self.m_index[movie_id]
        rated, ratings = self.R[u].indices, self.R[u].data
        sims = self.item_sim[m, rated]
        contrib = sims * preference_weight(ratings)
        order = np.argsort(-contrib)[:top]
        return [(int(self.movie_ids[rated[i]]), float(sims[i]), float(ratings[i])) for i in order if contrib[i] > 0]

    def pure_svd_scores(self, user_id: int, factors: int = 50) -> np.ndarray:
        """PureSVD (Cremonesi et al. 2010): strong top-N baseline, not explainable."""
        if not hasattr(self, "_svd_V"):
            from scipy.sparse.linalg import svds

            _, _, vt = svds(self.R.astype(np.float64), k=factors)
            self._svd_V = vt.T.astype(np.float32)
        u = self.u_index[user_id]
        return np.asarray(self.R[u] @ self._svd_V).ravel() @ self._svd_V.T

    def rated_mask(self, user_id: int) -> np.ndarray:
        mask = np.zeros(len(self.movie_ids), dtype=bool)
        mask[self.R[self.u_index[user_id]].indices] = True
        return mask
