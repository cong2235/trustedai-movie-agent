"""Offline evaluation helpers: temporal split, ranking metrics, rating-prediction metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config


@dataclass
class Split:
    train: pd.DataFrame
    test: pd.DataFrame


def temporal_split(ratings: pd.DataFrame, test_frac: float = 0.2, min_test: int = 2) -> Split:
    """Per user: the most recent `test_frac` of ratings go to test.

    Temporal (not random) because the product question is "what will they like next", and random
    splits leak future taste into training. Ties in timestamp (bulk rating sessions are common in
    MovieLens) are broken by movieId for determinism.
    """
    r = ratings.sort_values(["userId", "timestamp", "movieId"])
    rank = r.groupby("userId").cumcount(ascending=False)  # 0 = most recent
    n = r.groupby("userId")["movieId"].transform("size")
    n_test = np.maximum(min_test, np.floor(n * test_frac)).astype(int)
    is_test = rank < n_test
    return Split(train=r[~is_test].reset_index(drop=True), test=r[is_test].reset_index(drop=True))


def ranking_metrics(ranked: np.ndarray, relevant: set[int], k: int = 10) -> dict:
    top = list(ranked[:k])
    hits = [1.0 if m in relevant else 0.0 for m in top]
    dcg = sum(h / math.log2(i + 2) for i, h in enumerate(hits))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    first = next((i for i, h in enumerate(hits) if h), None)
    return {
        f"P@{k}": sum(hits) / k,
        f"R@{k}": sum(hits) / len(relevant),
        f"NDCG@{k}": dcg / idcg if idcg else 0.0,
        f"HR@{k}": 1.0 if any(hits) else 0.0,
        "MRR": 0.0 if first is None else 1.0 / (first + 1),
    }


def history_bucket(n: int) -> str:
    if n < 20:
        return "a) <20 train ratings"
    if n < 50:
        return "b) 20-49"
    if n < 150:
        return "c) 50-149"
    return "d) 150+"


def relevant_items(test: pd.DataFrame) -> dict[int, set[int]]:
    liked = test[test["rating"] >= config.LIKE_THRESHOLD]
    return liked.groupby("userId")["movieId"].apply(set).to_dict()
