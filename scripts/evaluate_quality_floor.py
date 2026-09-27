"""What does the quality floor cost in ranking accuracy, and what does it remove?

The floor (config.QUALITY_FLOOR_MEAN) drops movies with >= QUALITY_FLOOR_MIN_COUNT ratings whose raw mean is below
it. It was motivated by bad picks in *tone* requests (Maid to Order, 1.83 stars, in the held-out set), but it applies
to every recommendation, so its cost is measured here on the personal-ranking task: same temporal split and tuned
blend as scripts/evaluate_offline.py, top-10 with and without the floor, floor statistics from the training ratings.

    python scripts/evaluate_quality_floor.py   -> outputs/eval/quality_floor.md / .json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_offline import build, load_content  # noqa: E402

from movie_agent import config  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.evaluation import history_bucket, ranking_metrics, relevant_items, temporal_split  # noqa: E402

K = 10
FLOORS = [None, 2.5, 2.75, 3.0]


def main():
    t0 = time.time()
    full = MovieData.load()
    split = temporal_split(full.ratings, test_frac=0.2)
    rec = build(full, split.train, load_content(full))
    rec.weights = json.loads((config.OUTPUT_DIR / "eval" / "offline_metrics.json").read_text())["tuned_weights"]
    relevant = relevant_items(split.test)
    stats = rec.data.movie_stats
    means = stats["mean"].to_numpy()

    rows = []
    for u, rel in relevant.items():
        total, _, cand = rec.score(u)
        n_train = int(rec.cf.rated_mask(u).sum())
        for floor in FLOORS:
            mask = cand & (rec.quality_ok(floor) if floor else True)
            order = np.argsort(-np.where(mask, total, -np.inf))[:K]
            ids = rec.cf.movie_ids[order]
            m = ranking_metrics(ids, rel, K)
            m.update(
                floor=str(floor),
                user=u,
                bucket=history_bucket(n_train),
                mean_train_rating=float(np.nanmean(means[order])),
            )
            rows.append(m)
    df = pd.DataFrame(rows)

    summary = df.groupby("floor", sort=False)[[f"NDCG@{K}", f"HR@{K}", "mean_train_rating"]].mean()
    base = df[df.floor == "None"].set_index("user")[f"NDCG@{K}"]
    rng = np.random.default_rng(0)
    cis = {}
    for floor in FLOORS[1:]:
        d = (df[df.floor == str(floor)].set_index("user")[f"NDCG@{K}"] - base).to_numpy()
        boot = [rng.choice(d, len(d)).mean() for _ in range(2000)]
        cis[str(floor)] = {
            "delta_ndcg": float(d.mean()),
            "ci95": [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))],
        }
    judged = stats["count"].to_numpy() >= config.QUALITY_FLOOR_MIN_COUNT
    removed = {str(f): float((judged & (means < f)).sum() / judged.sum()) for f in FLOORS[1:]}
    by_bucket = df.pivot_table(index="bucket", columns="floor", values=f"NDCG@{K}", sort=False).round(4)

    md = [
        "# Quality floor: cost in ranking accuracy",
        "",
        f"Temporal split, tuned blend, {df.user.nunique()} users. Floor = drop movies with >= "
        f"{config.QUALITY_FLOOR_MIN_COUNT} training ratings and a raw mean below it. Shipped: {config.QUALITY_FLOOR_MEAN}.",
        "",
        summary.round(4).to_markdown(),
        "",
        "Paired bootstrap vs no floor (NDCG@10): "
        + "; ".join(f"{f}: {c['delta_ndcg']:+.4f} [{c['ci95'][0]:+.4f}, {c['ci95'][1]:+.4f}]" for f, c in cis.items()),
        "",
        "Share of judged movies (>= min count) removed: " + ", ".join(f"{f}: {v:.1%}" for f, v in removed.items()),
        "",
        "## NDCG@10 by history size",
        "",
        by_bucket.to_markdown(),
    ]
    out = config.OUTPUT_DIR / "eval"
    (out / "quality_floor.md").write_text("\n".join(md), encoding="utf-8")
    (out / "quality_floor.json").write_text(
        json.dumps(
            {"summary": summary.reset_index().to_dict(orient="records"), "vs_no_floor": cis, "removed_share": removed},
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(md))
    print(f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
