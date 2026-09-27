"""Offline tone / structure attributes per movie (moods, twist grade, violence grade).

Extracted once by scripts/extract_attributes.py (an LLM reads each plot; user tags are never shown to it) and
validated against user tags in scripts/evaluate_attributes.py. At query time they are plain arrays, so a request
like "light and funny, nothing violent" costs a few numpy operations instead of an LLM re-rank call.

Movies without attributes (unreliable plot, or not yet labelled) are treated as unknown: they get a neutral
score and are never removed by a violence filter, so missing data cannot silently hide a movie.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from . import config
from .data import MovieData

ATTRIBUTES_PATH = config.REPO_ROOT / "data" / "derived" / "movie_attributes.jsonl"
MOODS = (
    "funny",
    "light-hearted",
    "dark",
    "dark-comedy",
    "tense",
    "atmospheric",
    "emotional",
    "romantic",
    "inspiring",
    "thought-provoking",
    "surreal",
    "quirky",
    "satirical",
    "disturbing",
    "mind-bending",
    "action-packed",
    "family-friendly",
)


@dataclass
class MovieAttributes:
    moods: np.ndarray  # (n_movies, len(MOODS)) bool
    twist: np.ndarray  # (n_movies,) 0-3: how far the ending overturns the story (0 also for unknown)
    violence: np.ndarray  # (n_movies,) float, nan = unknown
    known: np.ndarray  # (n_movies,) bool: the movie has attributes at all

    @classmethod
    def load(cls, data: MovieData, path=ATTRIBUTES_PATH) -> MovieAttributes | None:
        if not path.exists():
            return None
        row_of = {int(m): i for i, m in enumerate(data.movies.index)}
        n = len(row_of)
        moods = np.zeros((n, len(MOODS)), dtype=bool)
        twist = np.zeros(n)
        violence = np.full(n, np.nan)
        known = np.zeros(n, dtype=bool)
        col = {m: j for j, m in enumerate(MOODS)}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            i = row_of.get(r["movie_id"])
            if i is None:
                continue
            known[i] = True
            for m in r["moods"]:
                if m in col:
                    moods[i, col[m]] = True
            twist[i] = r["twist"]
            if r["violence"] is not None:
                violence[i] = r["violence"]
        return cls(moods=moods, twist=twist, violence=violence, known=known)

    @staticmethod
    def check_moods(moods: list[str] | None) -> list[str]:
        bad = [m for m in moods or [] if m not in MOODS]
        if bad:
            raise ValueError(f"unknown moods {bad}; use: {', '.join(MOODS)}")
        return list(moods or [])

    def match(self, moods: list[str] | None = None, twist_ending: bool = False) -> np.ndarray:
        """Share of the requested attributes each movie has, in [0, 1] (the twist grade counts as grade / 3).
        Unknown movies get the mean of the known ones (neutral: neither promoted nor buried)."""
        wanted = [MOODS.index(m) for m in self.check_moods(moods)]
        parts = [self.moods[:, j].astype(float) for j in wanted]
        if twist_ending:
            parts.append(self.twist / 3.0)
        if not parts:
            return np.zeros(len(self.known))
        score = np.mean(parts, axis=0)
        score[~self.known] = score[self.known].mean() if self.known.any() else 0.0
        return score

    def violence_ok(self, max_violence: int | None) -> np.ndarray:
        """Mask of movies at or below the violence level; unknown violence passes."""
        if max_violence is None:
            return np.ones(len(self.known), dtype=bool)
        return np.isnan(self.violence) | (self.violence <= max_violence)

    def card(self, row: int) -> dict | None:
        if not self.known[row]:
            return None
        return {
            "moods": [MOODS[j] for j in np.where(self.moods[row])[0]],
            "twist_0_3": int(self.twist[row]),
            "violence_0_3": None if np.isnan(self.violence[row]) else int(self.violence[row]),
        }
