"""Independent audit of answers in saved transcripts, against the *raw dataset* rather than the tool outputs.

The online guardrail checks answers against what the tools returned. That cannot catch a tool that computed the
wrong thing. This audit recomputes the facts from ratings.csv / movies.csv and checks, per answer:
  * every "Title (Year)" exists in the dataset
  * "average rating X" / "X average" / "rated X on average" attached to a movie matches its dataset mean
    (claims about *similar users'* averages are separated: they come from neighbourhoods, not the global mean)
  * "N ratings" attached to a movie matches its dataset count
  * "you rated X N stars" matches the user's own rating
Everything that is not an exact match is listed with its sentence for manual review.

    python scripts/audit_answers.py outputs/transcripts_llm_gpt-4o-mini_memory
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.data import MovieData  # noqa: E402
from movie_agent.guardrails import TitleIndex, wrong_user_ratings  # noqa: E402

AVG = re.compile(
    r"(?:average(?: rating)?(?: of)?|avg\.?|rated)\s*(?:of\s*)?\**(\d\.\d{1,2})\**|\**(\d\.\d{1,2})\**\s*(?:★\s*)?(?:average|avg)",
    re.I,
)
COUNT = re.compile(r"(?<![\d.])(\d{1,4})\s+(?:user\s+)?ratings?\b", re.I)  # not the "0" of "5.0 rating"
# the user's own numbers: genre averages ("you have an average of 4.2") and ratings ("rated 5.0 by you")
OWN_AVG = re.compile(
    r"\byou(?:'ve| have)?\b[^.]{0,40}\baverage|\byour average|\bby you\b|\byou rated\b|"
    r"\byour (?:high )?(?:ratings?|love)\b",
    re.I,
)
NEIGHBOUR = re.compile(r"similar|like you|users with|neighbo|people who|taste", re.I)
USER_Q = re.compile(r"\(user (\d+)\)")


def sentences(text: str):
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        if s.strip():
            yield s.strip()


def audit_file(path: Path, data: MovieData, titles: TitleIndex) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    user = int(USER_Q.search(raw).group(1))
    issues, n_checked = [], 0
    stats = data.movie_stats
    for block in raw.split("**Assistant:**")[1:]:
        answer = re.split(r"\n> (?:PASS|FAIL)", block)[0]
        if "*(new session" in answer:
            answer = answer.split("---")[0]
        uid = user
        found, unknown = titles.mentions(answer)
        for u in unknown:
            issues.append({"file": path.name, "kind": "title not in dataset", "detail": u})
        subject = None
        for sent in sentences(answer):
            s_found, _ = titles.mentions(sent)
            # a bold title *opening* a list item / paragraph is its subject; a bold title mid-sentence is evidence
            # ("users who liked **The Shawshank Redemption** also appreciated this film")
            opens = sent.lstrip().startswith("**") or re.match(r"#+\s", sent.lstrip())
            bold = [m for pos, m in s_found if "**" in sent[pos : pos + 10]] if opens else []
            bold = bold[:1]
            if bold:
                subject = bold[0]  # a new list item / paragraph subject
            if not s_found and subject is None:
                continue
            # "this film has an average of 3.7" or a sentence naming only evidence movies -> the block subject;
            # otherwise the first movie the sentence names
            refers_back = re.search(r"\b(this|the) (film|movie|one)\b|\bit (has|holds|is rated)\b", sent, re.I)
            if subject is not None and (refers_back or not s_found or bold):
                mid = subject
            elif s_found:
                mid = s_found[0][1]
            else:
                continue
            label = data.label(mid)
            if not NEIGHBOUR.search(sent) and not OWN_AVG.search(sent):
                for m in AVG.finditer(sent):
                    x = float(m.group(1) or m.group(2))
                    n_checked += 1
                    true = stats.loc[mid, "mean"]
                    if true != true or abs(true - x) > 0.051:  # NaN-safe
                        issues.append(
                            {
                                "file": path.name,
                                "kind": "average differs from dataset",
                                "detail": f"{label}: says {x}, dataset mean {true:.2f} | {sent[:160]}",
                            }
                        )
            for m in COUNT.finditer(sent):
                n = int(m.group(1))
                n_checked += 1
                if NEIGHBOUR.search(sent[max(0, m.start() - 60) : m.end()]):
                    continue  # "10 similar users" is a neighbourhood count
                if n != int(stats.loc[mid, "count"]):
                    issues.append(
                        {
                            "file": path.name,
                            "kind": "rating count differs from dataset",
                            "detail": f"{label}: says {n}, dataset {int(stats.loc[mid, 'count'])} | {sent[:160]}",
                        }
                    )
        for w in wrong_user_ratings(answer, titles, uid):
            issues.append({"file": path.name, "kind": "user rating claim", "detail": w})
    return issues, n_checked


def main():
    folder = Path(sys.argv[1])
    data = MovieData.load()
    titles = TitleIndex.for_data(data)
    all_issues, total = [], 0
    for f in sorted(folder.glob("*.md")):
        issues, n = audit_file(f, data, titles)
        all_issues += issues
        total += n
    print(f"audited {len(list(folder.glob('*.md')))} transcripts, {total} numeric claims checked against the dataset")
    for i in all_issues:
        print(f"- [{i['kind']}] {i['file']}: {i['detail']}")
    if not all_issues:
        print("no discrepancies")


if __name__ == "__main__":
    main()
