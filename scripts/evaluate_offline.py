"""Offline evaluation of the recommendation engine and the "what do similar users think" predictor.

Protocol
  1. Temporal per-user split: oldest 80% train / newest 20% test.
     For tuning, the train part is split again (oldest 87.5% / next 12.5%) -> validation.
  2. Hybrid blend weights are tuned on validation only, then frozen and reported on test.
  3. Top-10 ranking over the full unseen catalogue; relevant = test rating >= 4.0.
  4. Rating prediction for the neighbour-opinion tool: RMSE/MAE vs. simple baselines,
     broken down by how many neighbours rated the movie (does "evidence_strength" mean anything?).

Outputs: outputs/eval/offline_metrics.json, outputs/eval/*.csv and a markdown summary.
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config  # noqa: E402
from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.content import ContentIndex  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.evaluation import history_bucket, ranking_metrics, relevant_items, temporal_split  # noqa: E402
from movie_agent.recommender import DEFAULT_WEIGHTS, SPARSE_USER_THRESHOLD, Recommender  # noqa: E402

OUT = config.OUTPUT_DIR / "eval"
K = 10
LONG_TAIL = 5


def load_content(data: MovieData) -> ContentIndex | None:
    try:
        return ContentIndex.load(data)
    except FileNotFoundError:
        print("!! embeddings not built - content signals disabled")
        return None


def build(full: MovieData, ratings: pd.DataFrame, content_full: ContentIndex | None) -> Recommender:
    data = full.with_ratings(ratings)
    content = None
    if content_full is not None:
        content = ContentIndex(
            data=data,
            movie_vecs=content_full.movie_vecs,
            chunk_vecs=content_full.chunk_vecs,
            chunk_owner=content_full.chunk_owner,
            tfidf=content_full.tfidf,
            tfidf_mat=content_full.tfidf_mat,
        )
    return Recommender(data=data, cf=CFModel(data), content=content)


def precompute_signals(rec: Recommender, users) -> dict[int, dict[str, np.ndarray]]:
    sig = {}
    for u in users:
        sig[u] = rec.signals(u)
    return sig


def evaluate_models(
    rec: Recommender, signals, relevant, models: dict[str, dict], diversify_models=()
) -> tuple[pd.DataFrame, dict]:
    """models: name -> blend weights over signal names. Returns per-user metric rows and rec lists."""
    cf, stats = rec.cf, rec.data.movie_stats
    counts = stats["count"].to_numpy()
    rows, lists = [], {name: {} for name in models}
    for u, rel in relevant.items():
        if u not in signals:
            continue
        n_train = int(cf.rated_mask(u).sum())
        for name, w in models.items():
            if "sparse" in w:
                w = w["sparse" if len(rec.data.user_ratings[u]) < SPARSE_USER_THRESHOLD else "rich"]
            total, _, cand = rec.score(u, weights=w, signals=signals[u])
            total = np.where(cand, total, -np.inf)
            order = np.argsort(-total)[: K * 5]
            if name in diversify_models:
                order = rec._mmr(list(order), total, K)
            ids = cf.movie_ids[np.asarray(order[:K])]
            m = ranking_metrics(ids, rel, K)
            tail_rel = {x for x in rel if counts[cf.m_index[x]] < LONG_TAIL}
            m.update(
                model=name,
                user=u,
                bucket=history_bucket(n_train),
                n_relevant=len(rel),
                tail_hits=len(tail_rel & set(ids)),
                n_tail_relevant=len(tail_rel),
                rec_mean_log_pop=float(np.log1p(counts[order[:K]]).mean()),
                rec_tail_share=float((counts[order[:K]] < LONG_TAIL).mean()),
            )
            rows.append(m)
            lists[name][u] = ids
    return pd.DataFrame(rows), lists


def list_level(rec: Recommender, lists: dict) -> pd.DataFrame:
    """Catalogue coverage and intra-list diversity (1 - mean pairwise plot similarity)."""
    out = []
    n_cat = len(rec.cf.movie_ids)
    for name, per_user in lists.items():
        all_ids = np.concatenate(list(per_user.values()))
        ild = np.nan
        if rec.content is not None:
            vals = []
            for ids in per_user.values():
                v = rec.content.movie_vecs[[rec.cf.m_index[m] for m in ids]]
                s = v @ v.T
                vals.append(1 - (s.sum() - len(ids)) / (len(ids) * (len(ids) - 1)))
            ild = float(np.mean(vals))
        out.append({"model": name, "coverage": len(set(all_ids)) / n_cat, "intra_list_diversity": ild})
    return pd.DataFrame(out).set_index("model")


SINGLE_SIGNAL_MODELS = {
    "Popularity": {"popularity": 1.0},
    "TopRated (Bayesian avg)": {"quality": 1.0},
    "PureSVD (k=50)": {"pure_svd": 1.0},
    "ItemKNN": {"item_knn": 1.0},
    "UserKNN": {"user_knn": 1.0},
    "Content (plot embeddings)": {"content": 1.0},
}


def tune(rec_val: Recommender, relevant_val, sig=None) -> tuple[dict, pd.DataFrame]:
    """Small grid over blend weights (item_knn fixed at 1 as the scale anchor), picking by NDCG@10."""
    users = list(relevant_val)
    sig = sig or precompute_signals(rec_val, users)
    grid = {
        "user_knn": [0.0, 0.3, 0.6, 1.0],
        "content": [0.0, 0.15, 0.3, 0.6],
        "quality": [0.0, 0.15],
        "popularity": [0.0, 0.2, 0.5],
        "pure_svd": [0.0, 0.25, 0.5],
    }
    if rec_val.content is None:
        grid["content"] = [0.0]
    results = []
    for combo in itertools.product(*grid.values()):
        w = {"item_knn": 1.0, **dict(zip(grid.keys(), combo))}
        df, _ = evaluate_models(rec_val, sig, relevant_val, {"cand": w})
        results.append({**w, "NDCG@10": df["NDCG@10"].mean(), "R@10": df["R@10"].mean()})
    res = pd.DataFrame(results).sort_values("NDCG@10", ascending=False)
    best = res.iloc[0][["item_knn", *grid.keys()]].to_dict()
    return {k: float(v) for k, v in best.items()}, res


def rating_prediction_eval(rec: Recommender, test: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Accuracy of the neighbour-opinion predictor vs. baselines, bucketed by evidence size."""
    cf, data = rec.cf, rec.data
    mu = data.global_mean
    stats = data.movie_stats
    rows = []
    for t in test.itertuples(index=False):
        if t.userId not in cf.u_index:
            continue
        u, m = cf.u_index[t.userId], cf.m_index[t.movieId]
        base = float(np.clip(cf.baseline(u, m), 0.5, 5))
        old = cf.predict_rating(t.userId, t.movieId, method="mean_centered")
        new = cf.predict_rating(t.userId, t.movieId, method="residual")
        rows.append(
            {
                "userId": t.userId,
                "movieId": t.movieId,
                "rating": t.rating,
                "n_neighbors": new["n_neighbors"],
                "global_mean": mu,
                "user_mean": float(cf.user_mean[u]),
                "item_bayes_mean": float(stats.loc[t.movieId, "bayes"]),
                "bias_baseline": base,
                "knn_v1_mean_centered": old["prediction"] if old["prediction"] is not None else base,
                "knn_v2_residual (shipped)": new["prediction"] if new["prediction"] is not None else base,
            }
        )
    df = pd.DataFrame(rows)
    df["evidence"] = pd.cut(
        df["n_neighbors"], [-1, 0, 2, 9, 1000], labels=["0 neighbours (fallback)", "1-2", "3-9", "10+"]
    )

    def rmse(col, frame):
        return float(np.sqrt(((frame[col] - frame["rating"]) ** 2).mean()))

    methods = [
        "global_mean",
        "user_mean",
        "item_bayes_mean",
        "bias_baseline",
        "knn_v1_mean_centered",
        "knn_v2_residual (shipped)",
    ]
    overall = pd.DataFrame(
        {
            m: {
                "RMSE": rmse(m, df),
                "MAE": float((df[m] - df["rating"]).abs().mean()),
                "like_accuracy": float(((df[m] >= 3.75) == (df["rating"] >= 4)).mean()),
            }
            for m in methods
        }
    ).T
    by_ev = df.groupby("evidence", observed=True).apply(
        lambda g: pd.Series(
            {
                "n": len(g),
                "RMSE_bias_baseline": rmse("bias_baseline", g),
                "RMSE_knn_v1": rmse("knn_v1_mean_centered", g),
                "RMSE_knn_v2": rmse("knn_v2_residual (shipped)", g),
            }
        ),
        include_groups=False,
    )
    return overall, by_ev


def main():
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    full = MovieData.load()
    content_full = load_content(full)

    split = temporal_split(full.ratings, test_frac=0.2)
    inner = temporal_split(split.train, test_frac=0.125)
    rec_val = build(full, inner.train, content_full)
    rel_val = relevant_items(inner.test)
    sig_val = precompute_signals(rec_val, list(rel_val))
    best_w, grid = tune(rec_val, rel_val, sig_val)
    grid.to_csv(OUT / "tuning_grid_validation.csv", index=False)
    print(f"tuned weights (validation NDCG@10={grid['NDCG@10'].iloc[0]:.4f}): {best_w}  [{time.time() - t0:.0f}s]")
    n_val = {u: len(rec_val.data.user_ratings[u]) for u in rel_val}
    seg_w, seg_val_ndcg = {}, 0.0
    for seg, pick in {
        "sparse": lambda n: n < SPARSE_USER_THRESHOLD,
        "rich": lambda n: n >= SPARSE_USER_THRESHOLD,
    }.items():
        rel_seg = {u: r for u, r in rel_val.items() if pick(n_val[u])}
        seg_w[seg], g = tune(rec_val, rel_seg, {u: sig_val[u] for u in rel_seg})
        g.to_csv(OUT / f"tuning_grid_validation_{seg}.csv", index=False)
        seg_val_ndcg += g["NDCG@10"].iloc[0] * len(rel_seg) / len(rel_val)
        print(f"  {seg} ({len(rel_seg)} users): {seg_w[seg]}")
    use_seg = bool(seg_val_ndcg > grid["NDCG@10"].iloc[0] + 0.002)
    print(
        f"  segment blend validation NDCG {seg_val_ndcg:.4f} vs global {grid['NDCG@10'].iloc[0]:.4f} -> use: {use_seg}"
    )

    rec = build(full, split.train, content_full)
    relevant = relevant_items(split.test)
    signals = precompute_signals(rec, list(relevant))
    models = dict(SINGLE_SIGNAL_MODELS)
    models["Hybrid (hand-set weights)"] = DEFAULT_WEIGHTS
    models["Hybrid w/o latent factors (tuned, pure_svd=0)"] = (
        grid[grid["pure_svd"] == 0].iloc[0][["item_knn", "user_knn", "content", "quality", "popularity"]].to_dict()
    )
    models["Hybrid (tuned on validation)"] = best_w
    models["Hybrid segment-tuned (sparse/rich)"] = seg_w
    models["Hybrid tuned + MMR diversity"] = best_w
    if content_full is None:
        models.pop("Content (plot embeddings)")
    per_user, lists = evaluate_models(rec, signals, relevant, models, diversify_models={"Hybrid tuned + MMR diversity"})
    per_user.to_csv(OUT / "per_user_metrics.csv", index=False)

    metric_cols = [f"P@{K}", f"R@{K}", f"NDCG@{K}", f"HR@{K}", "MRR"]
    overall = per_user.groupby("model")[metric_cols + ["rec_mean_log_pop", "rec_tail_share"]].mean()
    tail = per_user.groupby("model")[["tail_hits", "n_tail_relevant"]].sum()
    overall["tail_recall"] = tail["tail_hits"] / tail["n_tail_relevant"]
    overall = overall.join(list_level(rec, lists))
    overall = overall.loc[list(models)]
    by_bucket = per_user.pivot_table(index="bucket", columns="model", values=f"NDCG@{K}", aggfunc="mean")[list(models)]
    bucket_sizes = per_user[per_user["model"] == "ItemKNN"].groupby("bucket").size()
    by_bucket.insert(0, "n_users", bucket_sizes)

    hyb = per_user[per_user.model == "Hybrid (tuned on validation)"].set_index("user")[f"NDCG@{K}"]
    rng = np.random.default_rng(0)
    cis = {}
    for base in ["ItemKNN", "PureSVD (k=50)", "Popularity"]:
        other = per_user[per_user.model == base].set_index("user")[f"NDCG@{K}"].loc[hyb.index]
        diff = (hyb - other).to_numpy()
        boots = [rng.choice(diff, len(diff)).mean() for _ in range(2000)]
        cis[base] = {
            "mean_diff": float(diff.mean()),
            "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
        }

    pred_overall, pred_by_ev = rating_prediction_eval(rec, split.test)

    result = {
        "protocol": {
            "split": "per-user temporal 80/20; validation = next-to-last 12.5% of train",
            "relevance": f"test rating >= {config.LIKE_THRESHOLD}",
            "k": K,
            "n_test_users_with_relevant_items": len(relevant),
            "n_test_ratings": len(split.test),
            "long_tail_def": f"< {LONG_TAIL} train ratings",
        },
        "tuned_weights": best_w,
        "segment_weights": seg_w,
        "use_segment_weights": use_seg,
        "validation_ndcg": {"global_blend": float(grid["NDCG@10"].iloc[0]), "segment_blends": float(seg_val_ndcg)},
        "ranking_overall": overall.round(4).reset_index().to_dict(orient="records"),
        "ndcg_by_history_bucket": by_bucket.round(4).reset_index().to_dict(orient="records"),
        "hybrid_vs_baselines_ndcg_diff_bootstrap": cis,
        "rating_prediction_overall": pred_overall.round(4).reset_index(names="method").to_dict(orient="records"),
        "rating_prediction_by_evidence": pred_by_ev.round(4).reset_index().to_dict(orient="records"),
        "runtime_s": round(time.time() - t0, 1),
    }
    (OUT / "offline_metrics.json").write_text(json.dumps(result, indent=2))

    md = [
        "# Offline evaluation results",
        "",
        f"Protocol: {result['protocol']}",
        "",
        f"Tuned blend weights (validation): `{best_w}`",
        "",
        "## Top-10 ranking (test)",
        "",
        overall.round(4).to_markdown(),
        "",
        "## NDCG@10 by user history size",
        "",
        by_bucket.round(4).to_markdown(),
        "",
        "## Hybrid (tuned) minus baseline, NDCG@10, bootstrap 95% CI",
        "",
        pd.DataFrame(cis).T.to_markdown(),
        "",
        "## Rating prediction (neighbour-opinion tool)",
        "",
        pred_overall.round(4).to_markdown(),
        "",
        "### kNN error by evidence size",
        "",
        pred_by_ev.round(4).to_markdown(),
        "",
    ]
    (OUT / "offline_metrics.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
