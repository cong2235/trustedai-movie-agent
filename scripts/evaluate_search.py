"""Evaluate free-text search: embedding backend x re-ranker, on topic queries and on tone/structure queries.

Relevance labels come from user tags ("a courtroom trial drama" is relevant to movies tagged `court`). Tags
are sparse, so many truly relevant movies are unlabelled: absolute precision is a LOWER BOUND and only the
comparison between methods is meaningful. To avoid leakage, tags are removed from the lexical index and
hidden from the re-rankers during this evaluation.

Two query sets, because the earlier failure analysis showed they behave differently:
  TOPIC  what the movie is about (time travel, boxing, the mafia)
  TONE   how it feels or is built (a twist, dark comedy, surreal, funny)

    python scripts/evaluate_search.py                       # all available backends, rerankers none/cross/llm
    python scripts/evaluate_search.py --rerankers none cross
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config  # noqa: E402
from movie_agent.content import ContentIndex  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.embedders import BACKENDS, artifact_dir  # noqa: E402
from movie_agent.rerank import get_reranker  # noqa: E402

# query -> tags whose movies count as relevant
TOPIC = {
    "characters travel back in time and change history": {"time travel"},
    "an adventure set in outer space": {"space"},
    "aliens come to earth": {"aliens"},
    "a mob family and organized crime": {"mafia"},
    "a superhero with special powers fights a villain": {"superhero"},
    "a character struggling with mental illness": {"mental illness"},
    "teenagers dealing with life in high school": {"high school"},
    "a story about faith, god and religion": {"religion"},
    "political intrigue, elections and government": {"politics"},
    "reporters investigating a story for a newspaper": {"journalism"},
    "a boxer training for the big fight": {"boxing"},
    "soldiers in the Vietnam war": {"vietnam"},
    "a courtroom trial drama": {"court"},
    "a Stephen King adaptation": {"stephen king"},
    "robots and artificial intelligence": {"robots"},
    "a movie set at Christmas": {"christmas"},
    "an adaptation of a Shakespeare play": {"shakespeare"},
    "a film about racism and prejudice": {"racism"},
}
TONE = {
    "a thriller with a shocking twist ending": {"twist ending", "plot twist"},
    "a pitch-black dark comedy": {"dark comedy", "black comedy"},
    "a surreal, dreamlike film": {"surreal", "dreamlike"},
    "a moody, atmospheric film": {"atmospheric"},
    "a really funny movie that makes you laugh out loud": {"funny", "hilarious"},
    "a quirky, offbeat movie": {"quirky"},
    "a thought-provoking film that makes you think": {"thought-provoking", "philosophical"},
    "a tense, suspenseful film": {"suspense"},
    "a deeply emotional tearjerker": {"emotional"},
    "a disturbing, unsettling film": {"disturbing"},
    "a mind-bending psychological movie that messes with your head": {"mindfuck", "psychological"},
    "a satire that mocks society": {"satire", "spoof"},
}
K = 10


def ndcg(hits: list[bool], n_rel: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, h in enumerate(hits) if h)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(n_rel, len(hits))))
    return dcg / idcg if idcg else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", nargs="*", default=[b for b in BACKENDS if (artifact_dir(b) / "movie_vecs.npy").exists()])
    ap.add_argument("--rerankers", nargs="*", default=["none", "cross", "llm"])
    args = ap.parse_args()

    data = MovieData.load()
    tagged = data.tags.groupby("tag")["movieId"].apply(set).to_dict()
    ids = data.movies.index.to_numpy()
    qz = data.movie_stats["bayes"].to_numpy()
    qz = (qz - qz.mean()) / qz.std()
    rerankers = {}
    for name in args.rerankers:
        r = get_reranker(name)
        r.show_tags = False
        rerankers[name] = r

    rows, examples = [], {}
    for backend in args.backends:
        idx = ContentIndex.load(data, use_tags=False, backend=backend)
        for qset, queries in (("topic", TOPIC), ("tone", TONE)):
            for query, labels in queries.items():
                rel = set().union(*(tagged.get(t, set()) for t in labels))
                first = idx.relevance(query) + 0.35 * qz           # the shipped first stage (without taste)
                pool_rows = np.argsort(-first)[:config.RERANK_POOL]
                pool_ids = [int(ids[i]) for i in pool_rows]
                variants = {"dense only": [int(ids[i]) for i in np.argsort(-idx.relevance(query, "dense"))[:K]],
                            "stage 1 (dense+lexical+quality)": pool_ids[:K]}
                timings = {}
                for name, rr in rerankers.items():
                    if name == "none":
                        continue
                    res = rr.rerank(query, pool_ids, [float(first[i]) for i in pool_rows], data)
                    variants[f"stage 1 + {name} rerank"] = res.order[:K]
                    timings[f"stage 1 + {name} rerank"] = res.ms
                for method, top in variants.items():
                    hits = [m in rel for m in top]
                    rows.append({"backend": backend, "set": qset, "query": query, "method": method,
                                 f"P@{K}": float(np.mean(hits)), f"NDCG@{K}": ndcg(hits, len(rel)),
                                 "n_labelled": len(rel), "ms": timings.get(method, 0)})
                    if backend == args.backends[-1] and method == list(variants)[-1] and qset == "tone":
                        examples[query] = [("✓ " if h else "  ") + data.label(m) for m, h in zip(top[:5], hits[:5])]
                    if backend == args.backends[-1] and method == "stage 1 (dense+lexical+quality)" and qset == "tone":
                        examples.setdefault("__before__" + query, [("✓ " if h else "  ") + data.label(m)
                                                                   for m, h in zip(top[:5], hits[:5])])

    df = pd.DataFrame(rows)
    summary = df.pivot_table(index=["backend", "method"], columns="set", values=[f"P@{K}", f"NDCG@{K}"], aggfunc="mean")
    summary.columns = [f"{m} {s}" for m, s in summary.columns]
    order = ["dense only", "stage 1 (dense+lexical+quality)"] + [f"stage 1 + {r} rerank" for r in args.rerankers if r != "none"]
    summary = summary.reindex([(b, m) for b in args.backends for m in order if (b, m) in summary.index])
    latency = df[df["ms"] > 0].groupby("method")["ms"].median()

    out = config.OUTPUT_DIR / "eval"
    out.mkdir(parents=True, exist_ok=True)
    md = ["# Content search evaluation (tags as silver labels)", "",
          f"{len(TOPIC)} topic + {len(TONE)} tone/structure queries. Tags hidden from the lexical index and re-rankers. "
          "Precision is a lower bound (tags are sparse).", "",
          summary.round(3).to_markdown(), "", "Median re-rank latency (ms): " + ", ".join(f"{k}: {v:.0f}" for k, v in latency.items()), "",
          "## P@10 per tone query", "",
          df[df.set == "tone"].pivot_table(index="query", columns=["backend", "method"], values=f"P@{K}").round(2).to_markdown(), "",
          "## Tone queries: top-5 before / after re-ranking (last backend; ✓ = tagged)", ""]
    for q in TONE:
        if q in examples:
            md += [f"**{q}**", "", "| stage 1 | + re-rank |", "|---|---|"]
            md += [f"| {a} | {b} |" for a, b in zip(examples["__before__" + q], examples[q])] + [""]
    (out / "search_eval.md").write_text("\n".join(md), encoding="utf-8")
    df.to_csv(out / "search_eval_per_query.csv", index=False)
    (out / "search_eval.json").write_text(json.dumps({
        "summary": summary.round(4).reset_index().to_dict(orient="records"),
        "latency_ms": latency.to_dict()}, indent=2), encoding="utf-8")
    print("\n".join(md[:8]))


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"{time.time() - t0:.0f}s")
