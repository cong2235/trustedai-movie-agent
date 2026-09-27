"""Extract structured tone / structure attributes for every movie, once, offline.

Why: plot embeddings match what a movie is *about*, not how it *feels* (Failure 2 in the report). The shipped fix
was an LLM re-ranker at query time (+2.3 s median per request). This script runs the same kind of judgement once per
movie, so tone becomes a cheap, inspectable attribute that tools can filter and score on.

    python scripts/extract_attributes.py              # ~5,000 movies, gpt-4o-mini, resumable
    python scripts/extract_attributes.py --limit 50   # quick look

Output: data/derived/movie_attributes.jsonl (committed; one JSON object per movie).
The model sees title, year, genres and plot (start + ending, where twists are revealed) but never the user tags,
so the tags remain independent labels for validation (scripts/evaluate_attributes.py).
Movies whose plot is unreliable (duplicated across titles, see data.py) are skipped: their attributes would be
derived from another film's story.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.attributes import ATTRIBUTES_PATH, MOODS  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.telemetry import cost_usd  # noqa: E402

MODEL = "gpt-4o-mini"
BATCH = 10
HEAD_CHARS, TAIL_CHARS = 1200, 600

PROMPT = f"""You label movies with tone and structure attributes, judging mainly from the plot text given.
For each movie return:
  "moods": 1-4 labels from this list only: {", ".join(MOODS)}
  "twist": 0-3, how much the ending overturns what the audience believed about earlier events:
      0 = no reveal (most movies: a resolution, a death, a betrayal, a reconciliation or a chase is 0)
      1 = a surprising turn, but the story still means what it seemed to mean
      2 = a real reveal about a key character or event
      3 = the ending recontextualises the whole film (a character was dead all along, the narrator is the
          culprit, everything was staged or imagined)
  "twist_evidence": for twist >= 2, the few words of the plot that show the reveal; otherwise ""
  "violence": 0 none, 1 mild, 2 moderate, 3 graphic / pervasive
Use general film knowledge only to interpret the text, never to add attributes the text gives no sign of.
Return JSON: {{"movies": [{{"id": <id>, "moods": [...], "twist": <0-3>, "twist_evidence": "...", "violence": <0-3>}}, ...]}}"""


def movie_text(row) -> str:
    plot = row["plot"]
    if len(plot) > HEAD_CHARS + TAIL_CHARS:
        plot = plot[:HEAD_CHARS] + " [...] " + plot[-TAIL_CHARS:]
    return f"{row['display']} ({row['year']}) | genres: {', '.join(row['genres'])} | plot: {plot}"


def label_batch(client, batch: list[tuple[int, str]]) -> tuple[list[dict], dict]:
    listing = "\n\n".join(f"[{mid}] {text}" for mid, text in batch)
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": listing}],
    )
    wanted = {mid for mid, _ in batch}
    out = []
    for item in json.loads(resp.choices[0].message.content).get("movies", []):
        try:
            mid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if mid not in wanted:
            continue
        moods = [m for m in item.get("moods", []) if m in MOODS][:4]
        violence = item.get("violence")
        evidence = str(item.get("twist_evidence") or "").strip()
        twist = item.get("twist")
        twist = int(twist) if isinstance(twist, (int, float)) and 0 <= twist <= 3 else 0
        if twist >= 2 and not evidence:  # a reveal claimed without quoting the plot is downgraded
            twist = 1
        out.append(
            {
                "movie_id": mid,
                "moods": moods,
                "twist": twist,
                "twist_evidence": evidence[:200] if twist >= 2 else "",
                "violence": int(violence) if isinstance(violence, (int, float)) and 0 <= violence <= 3 else None,
            }
        )
    usage = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
    return out, usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    import openai

    client = openai.OpenAI(timeout=60.0, max_retries=3)
    data = MovieData.load()
    ATTRIBUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if ATTRIBUTES_PATH.exists():  # resumable: keep what is already labelled
        done = {
            json.loads(line)["movie_id"] for line in ATTRIBUTES_PATH.read_text(encoding="utf-8").splitlines() if line
        }
    todo = [
        (int(mid), movie_text(row)) for mid, row in data.movies.iterrows() if row["plot_ok"] and int(mid) not in done
    ]
    todo = todo[: args.limit] if args.limit else todo
    batches = [todo[i : i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"{len(done)} already labelled, {len(todo)} to go in {len(batches)} calls")

    t0, tokens, n_done, missing = time.time(), {"in": 0, "out": 0}, 0, 0
    with ThreadPoolExecutor(args.workers) as pool, ATTRIBUTES_PATH.open("a", encoding="utf-8") as f:
        futures = {pool.submit(label_batch, client, b): b for b in batches}
        for fut in as_completed(futures):
            try:
                rows, usage = fut.result()
            except Exception as e:  # a failed batch is simply picked up by the next run
                print(f"batch failed: {type(e).__name__}: {e}"[:200])
                continue
            for r in rows:
                f.write(json.dumps(r) + "\n")
            f.flush()
            missing += len(futures[fut]) - len(rows)
            n_done += len(rows)
            tokens = {k: tokens[k] + usage[k] for k in tokens}
            if n_done % 500 < BATCH:
                print(f"  {n_done}/{len(todo)}  {time.time() - t0:.0f}s")
    print(
        f"labelled {n_done}, missing from replies {missing} (re-run to fill), "
        f"tokens in/out {tokens['in']}/{tokens['out']}, cost ${cost_usd(MODEL, tokens['in'], tokens['out']):.2f}, "
        f"{time.time() - t0:.0f}s"
    )


if __name__ == "__main__":
    main()
