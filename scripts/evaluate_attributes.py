"""Validate the LLM-extracted movie attributes against user tags (which the extractor never saw).

Tags are sparse and missing-not-at-random: a movie without a "funny" tag may well be funny. So precision cannot be
measured. What can be measured, per attribute:
    recall  share of movies *tagged* with the concept that the extractor also labelled
    base    share of all labelled movies that carry the attribute
    lift    recall / base: how much more often the attribute appears on tagged movies than on a random movie
A useful attribute has high recall and a lift well above 1. Twist and violence are graded 0-3; they are reported
at each threshold, and as the mean grade of tagged vs all movies.

    python scripts/evaluate_attributes.py      -> outputs/eval/attributes_eval.md / .json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config  # noqa: E402
from movie_agent.attributes import MOODS, MovieAttributes  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402

# attribute -> tags that express the same concept
MOOD_TAGS = {
    "funny": {"funny", "hilarious", "very funny"},
    "dark-comedy": {"dark comedy", "black comedy", "dark humor"},
    "dark": {"dark"},
    "tense": {"suspense", "tense", "suspenseful", "intense"},
    "atmospheric": {"atmospheric"},
    "emotional": {"emotional", "touching", "sad"},
    "inspiring": {"inspirational", "inspiring", "feel-good"},
    "thought-provoking": {"thought-provoking", "philosophical"},
    "surreal": {"surreal", "dreamlike", "hallucinatory", "weird"},
    "quirky": {"quirky"},
    "satirical": {"satire", "spoof"},
    "disturbing": {"disturbing", "creepy"},
    "mind-bending": {"mindfuck", "mind-bending"},
    "action-packed": {"action", "action packed"},
    "family-friendly": {"family", "heartwarming"},
}
TWIST_TAGS = {"twist ending", "plot twist", "twist", "twists & turns"}
VIOLENCE_TAGS = {"violence", "violent", "bloody", "gore", "casual violence", "meaningless violence"}


def main():
    data = MovieData.load()
    attrs = MovieAttributes.load(data)
    if attrs is None:
        sys.exit("No attributes yet: run scripts/extract_attributes.py")
    row_of = {int(m): i for i, m in enumerate(data.movies.index)}
    tagged = data.tags.groupby("tag")["movieId"].apply(set).to_dict()

    def rows_with(tags: set[str]) -> np.ndarray:
        ids = set().union(*(tagged.get(t, set()) for t in tags))
        return np.array(sorted(row_of[m] for m in ids if m in row_of and attrs.known[row_of[m]]), dtype=int)

    known = attrs.known
    lines = ["| Attribute | Tagged movies (labelled) | Recall on tagged | Base rate | Lift |", "|---|---|---|---|---|"]
    result = {"n_labelled": int(known.sum()), "n_catalogue": int(len(known)), "moods": {}}
    for mood, tags in MOOD_TAGS.items():
        rows = rows_with(tags)
        col = attrs.moods[:, MOODS.index(mood)]
        base = float(col[known].mean())
        recall = float(col[rows].mean()) if len(rows) else float("nan")
        lift = recall / base if base else float("nan")
        result["moods"][mood] = {"n_tagged": int(len(rows)), "recall": recall, "base": base, "lift": lift}
        lines.append(f"| {mood} | {len(rows)} | {recall:.2f} | {base:.2f} | {lift:.1f}x |")

    graded = {}
    for name, tags, values in (
        ("twist", TWIST_TAGS, np.where(known, attrs.twist, np.nan)),
        ("violence", VIOLENCE_TAGS, attrs.violence),
    ):
        rows = rows_with(tags)
        ok = known & ~np.isnan(values)
        g = {
            "n_tagged": int(len(rows)),
            "mean_grade_tagged": float(np.nanmean(values[rows])) if len(rows) else None,
            "mean_grade_all": float(values[ok].mean()),
            "thresholds": {},
        }
        for t in (1, 2, 3):
            base = float((values[ok] >= t).mean())
            recall = float((values[rows] >= t).mean()) if len(rows) else float("nan")
            g["thresholds"][t] = {"recall": recall, "base": base, "lift": recall / base if base else float("nan")}
            lines.append(f"| {name} >= {t} | {len(rows)} | {recall:.2f} | {base:.2f} | {recall / base:.1f}x |")
        graded[name] = g
    result.update(graded)

    md = [
        "# Movie attributes vs user tags",
        "",
        f"{result['n_labelled']} of {result['n_catalogue']} movies labelled (the rest have an unreliable plot). "
        "Tags were never shown to the extractor. Recall = share of tagged movies that got the attribute; lift = "
        "recall / base rate. Precision is not measurable (a missing tag does not mean a missing attribute).",
        "",
    ]
    md += lines
    md += [
        "",
        f"Mean twist grade: tagged {graded['twist']['mean_grade_tagged']:.2f} vs all {graded['twist']['mean_grade_all']:.2f}. "
        f"Mean violence grade: tagged {graded['violence']['mean_grade_tagged']:.2f} vs all {graded['violence']['mean_grade_all']:.2f}.",
    ]
    out = config.OUTPUT_DIR / "eval"
    (out / "attributes_eval.md").write_text("\n".join(md), encoding="utf-8")
    (out / "attributes_eval.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
