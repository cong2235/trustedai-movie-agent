"""Does a graph / knowledge-graph approach help this problem? Measured on the same protocol as evaluate_offline.py.

Compares, on the same temporal split (params tuned on validation only, reported on test):
  * RP3beta on the rating graph
  * RP3beta on rating graph + knowledge nodes (genres, tags)   <- the "knowledge graph" variant
  * the shipped hybrid, and the hybrid with an RP3beta signal added to the blend
Reports NDCG@10 / HR@10, long-tail recall, popularity of recommendations and bootstrap CIs.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate_offline import (K, LONG_TAIL, build, evaluate_models, load_content,  # noqa: E402
                              precompute_signals)
from movie_agent import config  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.evaluation import relevant_items, temporal_split  # noqa: E402
from movie_agent.graph import RP3Beta  # noqa: E402

OUT = config.OUTPUT_DIR / "eval"


def add_graph_signal(rec, signals, name: str, model: RP3Beta) -> None:
    for u in signals:
        signals[u][name] = model.scores(u)


def ndcg_of(rec, signals, relevant, weights) -> float:
    df, _ = evaluate_models(rec, signals, relevant, {"m": weights})
    return float(df[f"NDCG@{K}"].mean())


def main():
    t0 = time.time()
    full = MovieData.load()
    content = load_content(full)
    hybrid_w = json.loads((OUT / "offline_metrics.json").read_text())["tuned_weights"]

    split = temporal_split(full.ratings, test_frac=0.2)
    inner = temporal_split(split.train, test_frac=0.125)

    # ---------------------------------------------------------- tune on validation
    rec_v = build(full, inner.train, content)
    rel_v = relevant_items(inner.test)
    sig_v = precompute_signals(rec_v, list(rel_v))
    grid_rows = []
    best = {}
    for kg in (0.0, 0.5, 2.0):
        best_kg = None
        for alpha, beta in itertools.product((0.8, 1.0), (0.3, 0.5, 0.7)):
            g = RP3Beta(rec_v.cf, alpha=alpha, beta=beta, kg_weight=kg).fit()
            add_graph_signal(rec_v, sig_v, "rp3", g)
            nd = ndcg_of(rec_v, sig_v, rel_v, {"rp3": 1.0})
            grid_rows.append({"kg_weight": kg, "alpha": alpha, "beta": beta, "val_NDCG@10": nd})
            if best_kg is None or nd > best_kg[0]:
                best_kg = (nd, alpha, beta)
        best[kg] = best_kg
        print(f"kg_weight={kg}: best val NDCG {best_kg[0]:.4f} (alpha={best_kg[1]}, beta={best_kg[2]})  [{time.time() - t0:.0f}s]")
    pd.DataFrame(grid_rows).to_csv(OUT / "graph_tuning_validation.csv", index=False)
    kg_best = max((k for k in best if k > 0), key=lambda k: best[k][0])

    # blend weight for the graph signal inside the hybrid, chosen on validation
    g_plain = RP3Beta(rec_v.cf, alpha=best[0.0][1], beta=best[0.0][2]).fit()
    g_kg = RP3Beta(rec_v.cf, alpha=best[kg_best][1], beta=best[kg_best][2], kg_weight=kg_best).fit()
    add_graph_signal(rec_v, sig_v, "rp3", g_plain)
    add_graph_signal(rec_v, sig_v, "rp3_kg", g_kg)
    blend = {}
    for sig in ("rp3", "rp3_kg"):
        cands = {w: ndcg_of(rec_v, sig_v, rel_v, {**hybrid_w, sig: w}) for w in (0.0, 0.25, 0.5, 1.0)}
        blend[sig] = max(cands, key=cands.get)
        print(f"hybrid + {sig}: validation {cands} -> weight {blend[sig]}")

    # ---------------------------------------------------------------- test
    rec = build(full, split.train, content)
    rel = relevant_items(split.test)
    sig = precompute_signals(rec, list(rel))
    t_fit = time.time()
    add_graph_signal(rec, sig, "rp3", RP3Beta(rec.cf, alpha=best[0.0][1], beta=best[0.0][2]).fit())
    add_graph_signal(rec, sig, "rp3_kg", RP3Beta(rec.cf, alpha=best[kg_best][1], beta=best[kg_best][2], kg_weight=kg_best).fit())
    fit_s = (time.time() - t_fit) / 2
    models = {
        "ItemKNN": {"item_knn": 1.0},
        "PureSVD (k=50)": {"pure_svd": 1.0},
        "RP3beta (rating graph)": {"rp3": 1.0},
        f"RP3beta + KG nodes (genres/tags, w={kg_best})": {"rp3_kg": 1.0},
        "Hybrid (shipped)": hybrid_w,
        f"Hybrid + RP3beta (w={blend['rp3']})": {**hybrid_w, "rp3": blend["rp3"]},
        f"Hybrid + RP3beta-KG (w={blend['rp3_kg']})": {**hybrid_w, "rp3_kg": blend["rp3_kg"]},
    }
    per_user, _ = evaluate_models(rec, sig, rel, models)
    cols = [f"NDCG@{K}", f"HR@{K}", f"R@{K}", "rec_mean_log_pop", "rec_tail_share"]
    table = per_user.groupby("model")[cols].mean()
    tail = per_user.groupby("model")[["tail_hits", "n_tail_relevant"]].sum()
    table["tail_recall"] = tail["tail_hits"] / tail["n_tail_relevant"]
    table = table.loc[list(models)]
    by_bucket = per_user.pivot_table(index="bucket", columns="model", values=f"NDCG@{K}")[list(models)]

    base = per_user[per_user.model == "Hybrid (shipped)"].set_index("user")[f"NDCG@{K}"]
    rng = np.random.default_rng(0)
    cis = {}
    for m in models:
        if m == "Hybrid (shipped)":
            continue
        diff = (per_user[per_user.model == m].set_index("user")[f"NDCG@{K}"].loc[base.index] - base).to_numpy()
        boots = [rng.choice(diff, len(diff)).mean() for _ in range(2000)]
        cis[m] = {"mean_diff_vs_hybrid": round(float(diff.mean()), 4),
                  "ci95": [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]}

    md = ["# Graph / knowledge-graph experiment", "",
          f"Same temporal split as the main eval; RP3beta params tuned on validation. Long tail = < {LONG_TAIL} train ratings. "
          f"Graph fit time ≈ {fit_s:.1f} s each.", "",
          "Validation-tuned params: " + ", ".join(f"kg_weight={k}: alpha={v[1]}, beta={v[2]} (val NDCG {v[0]:.4f})" for k, v in best.items()), "",
          "## Test results", "", table.round(4).to_markdown(), "",
          "## Difference vs shipped hybrid (NDCG@10, paired bootstrap 95% CI)", "", pd.DataFrame(cis).T.to_markdown(), "",
          "## NDCG@10 by user history", "", by_bucket.round(4).to_markdown(), ""]
    (OUT / "graph_eval.md").write_text("\n".join(md), encoding="utf-8")
    (OUT / "graph_eval.json").write_text(json.dumps({
        "best_params": {str(k): v for k, v in best.items()}, "blend_weights": blend,
        "test": table.round(4).reset_index().to_dict(orient="records"), "ci_vs_hybrid": cis,
        "by_bucket": by_bucket.round(4).reset_index().to_dict(orient="records")}, indent=2), encoding="utf-8")
    print("\n".join(md))
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
