"""Content signals: dense plot embeddings + sparse TF-IDF over plot/genres/tags.

Dense embeddings capture "vibe" ("a lonely robot finds love"); TF-IDF catches exact
entities and rare words ("Vietnam", "heist", "Stephen King") that small embedders blur.
Plots average ~3,200 chars, beyond the 512-token window, so each plot is chunked and
the movie vector is the normalised mean of its chunk vectors; search also uses the
best-matching chunk so a detail buried in the plot can still match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from . import config
from .data import MovieData
from .embedders import artifact_dir, get_embedder


def _chunks(text: str, size: int = config.CHUNK_WORDS, max_chunks: int = config.MAX_CHUNKS) -> list[str]:
    words = text.split()
    return [" ".join(words[i : i + size]) for i in range(0, len(words), size)][:max_chunks] or [""]


def build_embeddings(data: MovieData, backend: str | None = None) -> None:
    """Encode every plot chunk once and cache to artifacts/<backend>/."""
    embedder = get_embedder(backend)
    texts, owner = [], []
    for row_idx, (_mid, row) in enumerate(data.movies.iterrows()):
        header = f"{row['display']} ({row['year']}). Genres: {', '.join(row['genres'])}. "
        for j, chunk in enumerate(_chunks(row["plot"])):
            texts.append((header if j == 0 else "") + chunk)
            owner.append(row_idx)
    vecs = embedder.encode_docs(texts).astype(np.float32)
    owner = np.asarray(owner, dtype=np.int32)

    movie_vecs = np.zeros((len(data.movies), vecs.shape[1]), dtype=np.float32)
    np.add.at(movie_vecs, owner, vecs)
    movie_vecs /= np.linalg.norm(movie_vecs, axis=1, keepdims=True) + 1e-9

    out = artifact_dir(embedder.name)
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "chunk_vecs.npy", vecs)
    np.save(out / "chunk_owner.npy", owner)
    np.save(out / "movie_vecs.npy", movie_vecs)
    (out / "embeddings_meta.json").write_text(
        json.dumps(
            {
                "backend": embedder.name,
                "model": embedder.model_id,
                "dim": int(vecs.shape[1]),
                "n_movies": len(data.movies),
                "n_chunks": len(texts),
                "movie_ids": [int(m) for m in data.movies.index],
            }
        )
    )


@dataclass
class ContentIndex:
    data: MovieData
    movie_vecs: np.ndarray
    chunk_vecs: np.ndarray
    chunk_owner: np.ndarray
    tfidf: TfidfVectorizer
    tfidf_mat: object
    backend: str = config.EMBED_BACKEND
    _encoder: object = None

    @property
    def plot_ok(self) -> np.ndarray:
        return self.data.movies["plot_ok"].to_numpy()

    def _neutralise(self, scores: np.ndarray) -> np.ndarray:
        """Movies with an unreliable plot get the average score, so a wrong plot neither helps nor hurts."""
        ok = self.plot_ok
        out = scores.astype(np.float32).copy()
        out[~ok] = scores[ok].mean()
        return out

    @classmethod
    def load(cls, data: MovieData, use_tags: bool = True, backend: str | None = None) -> ContentIndex:
        """use_tags=False builds the lexical index from title/genres/plot only (used by the search eval,
        where tags are the relevance labels and indexing them would leak the answer)."""
        backend = backend or config.EMBED_BACKEND
        adir = artifact_dir(backend)
        meta_path = adir / "embeddings_meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"Embeddings for '{backend}' missing - run `python scripts/build_index.py --backend {backend}`."
            )
        meta = json.loads(meta_path.read_text())
        if meta["movie_ids"] != [int(m) for m in data.movies.index]:
            raise ValueError("Embedding cache is out of sync with movies_with_plots.csv - rebuild it.")

        docs = []
        for mid, row in data.movies.iterrows():
            tags = " ".join(data.movie_tags.get(mid, [])) if use_tags else ""
            plot = row["plot"] if row["plot_ok"] else ""
            docs.append(f"{row['display']} {' '.join(row['genres'])} {tags} {tags} {tags} {plot}")
        tfidf = TfidfVectorizer(stop_words="english", sublinear_tf=True, ngram_range=(1, 2), min_df=2, max_df=0.5)
        tfidf_mat = tfidf.fit_transform(docs)
        return cls(
            data=data,
            movie_vecs=np.load(adir / "movie_vecs.npy"),
            chunk_vecs=np.load(adir / "chunk_vecs.npy"),
            chunk_owner=np.load(adir / "chunk_owner.npy"),
            tfidf=tfidf,
            tfidf_mat=tfidf_mat,
            backend=backend,
        )

    def similar_to_movies(self, rows: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
        """Cosine similarity of every movie to the (weighted) centroid of `rows`."""
        w = np.ones(len(rows)) if weights is None else np.asarray(weights, dtype=np.float32)
        keep = self.plot_ok[rows]
        if not keep.any():
            return np.zeros(len(self.movie_vecs), dtype=np.float32)
        centroid = (self.movie_vecs[rows[keep]] * w[keep][:, None]).sum(0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        return self._neutralise(self.movie_vecs @ centroid)

    def encode_query(self, query: str) -> np.ndarray:
        if self._encoder is None:
            self._encoder = get_embedder(self.backend)
        cache = self.__dict__.setdefault("_qcache", {})
        if query not in cache:
            cache[query] = self._encoder.encode_query(query)
        return cache[query]

    def dense_scores(self, query: str) -> np.ndarray:
        q = self.encode_query(query)
        chunk_sim = self.chunk_vecs @ q
        best_chunk = np.full(len(self.movie_vecs), -1.0, dtype=np.float32)
        np.maximum.at(best_chunk, self.chunk_owner, chunk_sim)
        return self._neutralise(0.5 * best_chunk + 0.5 * (self.movie_vecs @ q))

    def lexical_scores(self, query: str) -> np.ndarray:
        return (self.tfidf_mat @ self.tfidf.transform([query]).T).toarray().ravel()

    def relevance(self, query: str, mode: str = "hybrid", lexical_weight: float = 0.1) -> np.ndarray:
        """Query relevance for every movie, z-scored so dense and lexical are on one scale."""

        def z(x):
            return (x - x.mean()) / (x.std() + 1e-9)

        if mode == "dense":
            return z(self.dense_scores(query))
        if mode == "lexical":
            return z(self.lexical_scores(query))
        return (1 - lexical_weight) * z(self.dense_scores(query)) + lexical_weight * z(self.lexical_scores(query))
