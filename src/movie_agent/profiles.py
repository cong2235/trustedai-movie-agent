"""User taste profiles: what a user rates, how generously, which genres they over/under-index on."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .cf import CFModel
from .data import MovieData


NON_GENRES = {"IMAX"}  # a screening format, not a taste


def population_genre_share(data: MovieData) -> pd.Series:
    """Average over users of 'fraction of my rated movies that are genre g'.

    Averaged per user (not pooled over ratings) so that the 1,900-rating users don't define "normal",
    and on the same scale as a single user's share (a movie can count toward several genres).
    """
    cache = data.__dict__.setdefault("_pop_genre_share", {})
    if "v" not in cache:
        r = data.ratings.merge(data.movies[["genres"]], left_on="movieId", right_index=True)
        n_user = r.groupby("userId").size()
        per_user = r.explode("genres").groupby(["userId", "genres"]).size().unstack(fill_value=0)
        cache["v"] = per_user.div(n_user, axis=0).mean()
    return cache["v"]


def genre_affinity(data: MovieData, user_id: int) -> pd.DataFrame:
    """Per-genre stats for one user versus the whole population.

    - share / global_share: fraction of their rated movies in this genre vs. the average user's
    - lift: share / global_share (>1 = over-indexes)
    - avg_rating and rel_rating: their mean in the genre, and that mean minus their overall mean,
      shrunk toward 0 when they have rated few movies of the genre (so 1 lucky 5-star isn't "love").
    """
    ur = data.user_ratings[user_id]
    genres = data.movies.loc[ur.index, "genres"]
    user_mean = float(ur.mean())
    rows = []
    global_counts = population_genre_share(data)
    for g in data.all_genres:
        if g in NON_GENRES:
            continue
        mask = genres.apply(lambda gs, g=g: g in gs)
        n = int(mask.sum())
        avg = float(ur[mask].mean()) if n else np.nan
        shrink = n / (n + 3)
        rows.append({
            "genre": g, "n_rated": n,
            "share": n / len(ur),
            "global_share": float(global_counts.get(g, 0.0)),
            "avg_rating": avg,
            "rel_rating": (avg - user_mean) * shrink if n else 0.0,
        })
    df = pd.DataFrame(rows).set_index("genre")
    df["lift"] = df["share"] / df["global_share"].clip(lower=1e-6)
    return df


def user_profile(data: MovieData, user_id: int, top: int = 8) -> dict:
    ur = data.user_ratings[user_id].sort_values(ascending=False)
    ts = data.ratings.loc[data.ratings["userId"] == user_id, "timestamp"]
    aff = genre_affinity(data, user_id)
    liked = aff[(aff["n_rated"] >= 3)].sort_values("rel_rating", ascending=False)
    years = data.movies.loc[ur.index, "year"]

    def cards(ids):
        return [{"title": data.label(m), "your_rating": float(ur[m])} for m in ids]

    return {
        "user_id": user_id,
        "n_ratings": int(len(ur)),
        "avg_rating": round(float(ur.mean()), 2),
        "rating_std": round(float(ur.std(ddof=0)), 2),
        "generosity_vs_population": round(float(ur.mean()) - data.global_mean, 2),
        "history_size": "sparse" if len(ur) < 30 else "moderate" if len(ur) < 150 else "rich",
        "active_period": f"{pd.to_datetime(ts.min(), unit='s').date()} to {pd.to_datetime(ts.max(), unit='s').date()}",
        "favourite_decades": (years // 10 * 10).value_counts().head(3).index.astype(int).astype(str).map(lambda d: d + "s").tolist(),
        "top_rated": cards(ur.index[:top]),
        "lowest_rated": cards(ur.index[::-1][:min(5, max(0, len(ur) - top))]),
        "most_watched_genres": [
            {"genre": g, "n_rated": int(r["n_rated"]), "share": round(r["share"], 2), "lift_vs_population": round(r["lift"], 2)}
            for g, r in aff.sort_values("n_rated", ascending=False).head(5).iterrows()
        ],
        "genres_rated_above_own_average": [
            {"genre": g, "avg_rating": round(r["avg_rating"], 2), "n_rated": int(r["n_rated"])}
            for g, r in liked.head(4).iterrows() if r["rel_rating"] > 0.05
        ],
        "genres_rated_below_own_average": [
            {"genre": g, "avg_rating": round(r["avg_rating"], 2), "n_rated": int(r["n_rated"])}
            for g, r in liked.tail(3)[::-1].iterrows() if r["rel_rating"] < -0.05
        ],
    }


def blind_spots(data: MovieData, cf: CFModel, user_id: int, top: int = 5) -> dict:
    """Genres the user under-watches relative to the population *and* that their taste-neighbours enjoy.

    A blind spot is only interesting if there is reason to think they'd like it, so each candidate
    genre is scored by (1 - lift) exposure gap x neighbours' relative liking of that genre, and we
    attach the neighbours' best-rated titles in it as concrete entry points.
    """
    aff = genre_affinity(data, user_id)
    seen = set(data.user_ratings[user_id].index)
    neigh = cf.similar_users(user_id, k=config.NEIGHBORHOOD_K)
    neigh_ids = [n[0] for n in neigh]
    weights = {n[0]: n[1] for n in neigh}
    nr = data.ratings[data.ratings["userId"].isin(neigh_ids)].merge(
        data.movies[["genres"]], left_on="movieId", right_index=True)
    nr["w"] = nr["userId"].map(weights)
    nr["dev"] = nr["rating"] - nr["userId"].map(nr.groupby("userId")["rating"].mean())
    exploded = nr.explode("genres")

    out = []
    for g, r in aff.iterrows():
        if r["global_share"] < 0.01 or r["lift"] >= 0.8:
            continue
        gx = exploded[exploded["genres"] == g]
        if len(gx) < 5:
            continue
        neigh_rel = float((gx["dev"] * gx["w"]).sum() / (gx["w"].sum() + 1e-9))
        unseen = gx[~gx["movieId"].isin(seen)]
        agg = unseen.groupby("movieId").agg(n=("rating", "size"), avg=("rating", "mean"))
        agg = agg[agg["n"] >= 2].sort_values(["avg", "n"], ascending=False).head(3)
        out.append({
            "genre": g,
            "your_n_rated": int(r["n_rated"]),
            "your_share": round(r["share"], 3),
            "population_share": round(r["global_share"], 3),
            "exposure_lift": round(r["lift"], 2),
            "your_avg_in_genre": None if r["n_rated"] == 0 else round(r["avg_rating"], 2),
            "similar_users_relative_liking": round(neigh_rel, 2),
            "score": round((1 - min(r["lift"], 1)) * (0.5 + neigh_rel), 3),
            "entry_points_liked_by_similar_users": [
                # both averages, explicitly named: the model once reported the 2-user neighbourhood average (5.0)
                # as the movie's "average rating" (dataset: 4.03) - caught by scripts/audit_answers.py
                {"title": data.label(m), "avg_among_your_similar_users": round(a["avg"], 2),
                 "n_similar_users_who_rated_it": int(a["n"]),
                 "avg_rating_all_users": round(float(data.movie_stats.loc[m, "mean"]), 2),
                 "n_ratings_all_users": int(data.movie_stats.loc[m, "count"])}
                for m, a in agg.iterrows()
            ],
        })
    out.sort(key=lambda d: -d["score"])
    return {"user_id": user_id, "n_similar_users_used": len(neigh), "blind_spots": out[:top],
            "method": "genres where your share of ratings is <80% of the population's, ranked by exposure gap "
                      "x how much your most similar users like the genre relative to their own average"}
