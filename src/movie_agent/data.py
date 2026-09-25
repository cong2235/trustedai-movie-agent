"""Loading, cleaning and per-movie statistics.

Everything downstream works with integer movieIds; titles are only for display and lookup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from . import config

_ARTICLE_RE = re.compile(r"^(?P<body>.+), (?P<art>The|A|An|Les|La|Le|Il|Das|Der|Die|El|L')$")


def display_title(raw: str) -> str:
    """'Usual Suspects, The' -> 'The Usual Suspects'; strips alternate-language titles in parentheses."""
    main = re.sub(r"\s*\(.*\)\s*$", "", raw).strip() or raw
    m = _ARTICLE_RE.match(main)
    if m:
        art = m.group("art")
        main = f"{art}{'' if art.endswith(chr(39)) else ' '}{m.group('body')}"
    return main


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_NUMWORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
             "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13"}


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and leading articles, and canonicalise sequel markers so that
    'The Godfather: Part II', 'godfather 2', "Ocean's Twelve" / "ocean's 12" and 'Alien³' / 'alien 3' line up.
    Standalone 'i' and 'x' are left alone ('I, Robot', 'X-Men')."""
    text = text.lower().replace("³", " 3").replace("²", " 2")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"^\s*(the|a|an)\s+", "", text)  # leading articles make every title look alike
    tokens = [_ROMAN.get(t, _NUMWORDS.get(t, t)) for t in text.split() if t != "part"]
    return " ".join(tokens)


def _numbers(norm: str) -> set[str]:
    return {t for t in norm.split() if t.isdigit()}


@dataclass
class MovieData:
    movies: pd.DataFrame          # indexed by movieId: title, year, genres(list), plot, display
    ratings: pd.DataFrame         # userId, movieId, rating, timestamp
    tags: pd.DataFrame            # userId, movieId, tag (cleaned, lowercased)
    _title_keys: list[str] = field(default_factory=list, repr=False)
    _title_ids: list[int] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, data_dir=config.DATA_DIR) -> "MovieData":
        movies = pd.read_csv(data_dir / "movies_with_plots.csv")
        movies["genres"] = movies["genres"].str.split("|").apply(
            lambda gs: [g for g in gs if g != "(no genres listed)"]
        )
        movies["display"] = movies["title"].map(display_title)
        # Data-quality guard: 198 movies in 41 groups share a byte-identical plot (a join error upstream;
        # e.g. "Twelve Monkeys" carries Mighty Aphrodite's plot). The true owner can't be recovered reliably,
        # so these plots are excluded from every content signal and never shown as the movie's plot.
        movies["plot_ok"] = ~movies["plot"].duplicated(keep=False)
        movies = movies.set_index("movieId")

        ratings = pd.read_csv(data_dir / "ratings.csv")
        tags = pd.read_csv(data_dir / "tags.csv")
        tags["tag"] = tags["tag"].astype(str).str.strip().str.lower()
        tags = tags[~tags["tag"].isin(config.NOISE_TAGS)]
        return cls(movies=movies, ratings=ratings, tags=tags)

    def with_ratings(self, ratings: pd.DataFrame) -> "MovieData":
        """Same catalogue, different ratings (used for train/test splits)."""
        return MovieData(movies=self.movies, ratings=ratings.reset_index(drop=True), tags=self.tags)

    # ------------------------------------------------------------ statistics
    @cached_property
    def global_mean(self) -> float:
        return float(self.ratings["rating"].mean())

    @cached_property
    def movie_stats(self) -> pd.DataFrame:
        """count, mean, bayes (shrunk mean) for every movie in the catalogue (0 ratings -> global mean)."""
        g = self.ratings.groupby("movieId")["rating"].agg(["count", "mean", "std"])
        g = g.reindex(self.movies.index)
        g["count"] = g["count"].fillna(0).astype(int)
        c, m = config.BAYES_PRIOR_COUNT, self.global_mean
        g["bayes"] = (g["mean"].fillna(m) * g["count"] + c * m) / (g["count"] + c)
        return g

    @cached_property
    def movie_tags(self) -> dict[int, list[str]]:
        """movieId -> tags ordered by how many users applied them."""
        counts = self.tags.groupby(["movieId", "tag"]).size().rename("n").reset_index()
        counts = counts.sort_values(["movieId", "n"], ascending=[True, False])
        return counts.groupby("movieId")["tag"].apply(list).to_dict()

    @cached_property
    def all_genres(self) -> list[str]:
        return sorted({g for gs in self.movies["genres"] for g in gs})

    @cached_property
    def user_ratings(self) -> dict[int, pd.Series]:
        """userId -> Series(movieId -> rating)."""
        return {u: grp.set_index("movieId")["rating"] for u, grp in self.ratings.groupby("userId")}

    def has_user(self, user_id: int) -> bool:
        return user_id in self.user_ratings

    # ----------------------------------------------------------- formatting
    def label(self, movie_id: int) -> str:
        row = self.movies.loc[movie_id]
        return f"{row['display']} ({row['year']})"

    def card(self, movie_id: int, plot_chars: int = 0) -> dict:
        """Compact, JSON-serialisable description of a movie used in every tool output."""
        row, st = self.movies.loc[movie_id], self.movie_stats.loc[movie_id]
        out = {
            "movie_id": int(movie_id),
            "title": self.label(movie_id),
            "genres": row["genres"],
            "n_ratings": int(st["count"]),
            "avg_rating": None if st["count"] == 0 else round(float(st["mean"]), 2),
        }
        tags = self.movie_tags.get(movie_id)
        if tags:
            out["tags"] = tags[:6]
        if not row["plot_ok"]:
            out["plot_unreliable"] = True
            if plot_chars:
                out["plot"] = "[unavailable: the source dataset attaches another movie's plot to this title]"
        elif plot_chars:
            plot = row["plot"]
            out["plot"] = plot if len(plot) <= plot_chars else plot[:plot_chars].rsplit(" ", 1)[0] + " ..."
        return out

    # ---------------------------------------------------------- title lookup
    def _build_title_index(self) -> None:
        keys, ids = [], []
        for mid, row in self.movies.iterrows():
            for variant in {row["display"], row["title"]}:
                keys.append(_normalize(variant))
                ids.append(int(mid))
        self._title_keys, self._title_ids = keys, ids

    def find_movie(self, query: str, limit: int = 5) -> list[dict]:
        """Fuzzy title search. Handles 'the usual suspects', 'Alice in Wonderland 2010', typos.

        Returns candidates with a score in [0, 100]; >= TITLE_MATCH_CONFIDENT is treated as a match.
        """
        if not self._title_keys:
            self._build_title_index()
        year = None
        years = list(re.finditer(r"\b(18|19|20)\d{2}\b", query))
        if years and years[-1].start() > 0:      # the last one: "2001: A Space Odyssey 1968" -> 1968
            m = years[-1]
            year = int(m.group(0))
            query = query[:m.start()] + query[m.end():]
        q = _normalize(query.replace("(", " ").replace(")", " "))
        q_nums = _numbers(q)
        hits = process.extract(q, self._title_keys, scorer=fuzz.WRatio, limit=max(limit * 8, 40))
        best: dict[int, float] = {}
        for _, score, idx in hits:
            mid = self._title_ids[idx]
            # exact normalized match beats partial-ratio matches ("Alien" vs "Aliens")
            key = self._title_keys[idx]
            score = 100.0 if key == q else min(score, 99.0) * 0.97
            # a title much shorter than the query is only a fragment of it: "boy" inside "oldboy",
            # "goon" inside "goonies", "m" inside "matrix" - partial-ratio scores those near 100
            if key != q and len(key) < 0.8 * len(q):
                score *= (len(key) / len(q)) ** 0.5
            # a number in the query is a sequel/part marker: "Terminator 2" must not resolve to The Terminator
            if q_nums and not q_nums <= _numbers(key):
                score *= 0.7
            elif q_nums and set(q.split()) <= set(key.split()):
                score = max(score, 95.0)     # "terminator 2" is fully contained in "terminator 2 judgment day"
            best[mid] = max(best.get(mid, 0), score)

        if year is not None:
            # the user named a year: a title from a different year is at best a guess (Solaris 1972 vs 2002)
            for mid in best:
                if int(self.movies.loc[mid, "year"]) != year:
                    best[mid] = min(best[mid], 84.0)

        def rank_key(kv):
            mid, score = kv
            year_match = year is not None and int(self.movies.loc[mid, "year"]) == year
            return (-score, not year_match, -self.movie_stats.loc[mid, "count"])

        ranked = sorted(best.items(), key=rank_key)
        return [{"movie_id": mid, "title": self.label(mid), "score": round(s, 1)}
                for mid, s in ranked[:limit]]

def genre_matrix(movies: pd.DataFrame, genres: list[str]) -> np.ndarray:
    """Binary (n_movies, n_genres) matrix aligned with movies.index."""
    idx = {g: i for i, g in enumerate(genres)}
    mat = np.zeros((len(movies), len(genres)), dtype=np.float32)
    for row, gs in enumerate(movies["genres"]):
        for g in gs:
            mat[row, idx[g]] = 1.0
    return mat
