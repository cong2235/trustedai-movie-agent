"""Graph-based recommendation, used to answer "would a knowledge graph help here?" with measurements.

RP3beta (Paudel et al. 2017; Dacrema et al. 2019 found it among the strongest simple baselines on MovieLens):
a 3-step random walk user -> movie -> user -> movie on the bipartite rating graph, with transition
probabilities raised to alpha and the landing movie's popularity divided out (^beta) to fight popularity bias.

"KG" variant: the graph is extended with knowledge nodes - one node per genre and per tag - linked to
their movies. A walk can then go movie -> genre/tag -> movie, reaching movies that few users rated
(the long tail where plain CF has recall ~0). Knowledge edges are weighted by `kg_weight` relative to
rating edges. This is a lightweight knowledge graph built from the data we have; a richer one (directors,
cast, franchises) would need external data via links.csv.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from .cf import CFModel


def _row_normalize(m: sp.csr_matrix) -> sp.csr_matrix:
    s = np.asarray(m.sum(1)).ravel()
    s[s == 0] = 1
    return sp.diags(1 / s) @ m


@dataclass
class RP3Beta:
    cf: CFModel
    alpha: float = 1.0
    beta: float = 0.5
    topk: int = 200
    kg_weight: float = 0.0
    min_rating: float = 0.5

    def fit(self) -> RP3Beta:
        R = self.cf.R.copy()
        R.data = (R.data >= self.min_rating).astype(np.float32)
        R.eliminate_zeros()
        blocks = [R]
        if self.kg_weight > 0:
            blocks.append(self._knowledge_rows() * self.kg_weight)
        G = sp.vstack(blocks).tocsr()
        Pui = _row_normalize(G)
        Piu = _row_normalize(G.T.tocsr())
        if self.alpha != 1.0:
            Pui = Pui.power(self.alpha)
            Piu = Piu.power(self.alpha)
        W = (Piu @ Pui).toarray().astype(np.float32)
        pop = np.asarray(G.sum(0)).ravel()
        pop[pop == 0] = 1
        W /= np.power(pop, self.beta)[None, :]
        np.fill_diagonal(W, 0)
        if self.topk and self.topk < W.shape[1]:
            cut = np.argpartition(-W, self.topk, axis=1)[:, self.topk :]
            np.put_along_axis(W, cut, 0.0, axis=1)
        self.W = W
        self.Pui_users = _row_normalize(R)
        return self

    def _knowledge_rows(self) -> sp.csr_matrix:
        """One row per genre and per tag: a 'knowledge node' connected to its movies."""
        data = self.cf.data
        m_index = self.cf.m_index
        rows, cols = [], []
        node = 0
        for g in data.all_genres:
            for mid, gs in zip(data.movies.index, data.movies["genres"]):
                if g in gs:
                    rows.append(node)
                    cols.append(m_index[int(mid)])
            node += 1
        for _tag, mids in data.tags.groupby("tag")["movieId"].apply(set).items():
            if len(mids) < 2:
                continue
            for mid in mids:
                rows.append(node)
                cols.append(m_index[int(mid)])
            node += 1
        return sp.csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, cols)), shape=(node, len(self.cf.movie_ids)))

    def scores(self, user_id: int) -> np.ndarray:
        u = self.cf.u_index[user_id]
        return np.asarray(self.Pui_users[u] @ self.W).ravel()

    def paths(self, user_id: int, movie_id: int, top: int = 3) -> list[tuple[int, float]]:
        """Explanation as graph paths: the user's movies j with the largest walk mass j -> movie_id."""
        u, m = self.cf.u_index[user_id], self.cf.m_index[movie_id]
        rated = self.cf.R[u].indices
        contrib = self.W[rated, m]
        order = np.argsort(-contrib)[:top]
        return [(int(self.cf.movie_ids[rated[i]]), float(contrib[i])) for i in order if contrib[i] > 0]
