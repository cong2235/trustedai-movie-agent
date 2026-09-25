"""Grounding checks on a finished answer, used both offline (scenario suite) and online (every live turn).

    hallucinated_titles     "Title (Year)" strings that match no movie in the dataset
    ungrounded_titles       real movies the answer names that no tool output in this conversation contains
    ungrounded_numbers      decimal numbers (ratings, averages, similarities) that no tool output contains
    misattributed_numbers   a number written next to movie X that the tools reported for a *different* movie
    wrong_user_ratings      "you rated/gave X N stars" where N is not the user's actual rating of X (checked
                            against the dataset itself, so it also covers integers like "5 stars")

Scope and limits: movies are recognised when written as "Title (Year)" (the system prompt requires it);
qualitative claims ("similar users loved it") are not verifiable this way and are left to review.
Online, a failed check triggers one revision round and is logged.
"""

from __future__ import annotations

import json
import re

from .data import MovieData, _normalize

YEAR_PAREN = re.compile(r"\((\d{4})\)")
DECIMAL = re.compile(r"(?<![\d.])(\d\.\d{1,2})(?!\d|\.\d)")   # "4.9." at a sentence end still counts
# "you rated X 5 stars", "you gave it a 3.5", "you rated both X and Y 5★".
# v1 false positives, now excluded: "a user similar *to you* rated it 4.5" (lookbehinds), "you rated Terminator 2 and
# Alien highly" (a bare number must carry a unit or follow "a"), "..., with a predicted rating of 4.8" (clause stops).
USER_RATING_CLAIM = re.compile(
    r"(?<!to )(?<!like )(?<!than )(?<!with )(?<!as )\byou(?:'ve| have)?\s+(?:rated|gave|give)\b"
    r"(?P<inner>[^.!?;\n]{0,160}?)"
    r"(?:(?P<art>\ban?\s+\**)|(?<![\d.]))(?P<n>[0-5](?:\.\d)?)(?![\d.]\d)\**"
    r"(?P<unit>\s*(?:stars?|★|/\s*5|out of 5))?", re.I)
OTHER_RATER = re.compile(r"similar|users?\b|others|people|average|avg|predicted|neighbou?rs|everyone", re.I)
TOL = 0.05


def _close(x: float, values) -> bool:
    return any(abs(x - v) <= TOL + 1e-9 or abs(x - 100 * v) <= TOL + 1e-9 for v in values)


class TitleIndex:
    @classmethod
    def for_data(cls, data: MovieData) -> "TitleIndex":
        """Shared per dataset: building it walks all 5,135 movies, which each new chat session used to repeat."""
        return data.__dict__.setdefault("_guardrail_title_index", cls(data))

    def __init__(self, data: MovieData):
        self.data = data
        self.index = {(_normalize(row["display"]), int(row["year"])): int(mid) for mid, row in data.movies.iterrows()}

    def mentions(self, text: str) -> tuple[list[tuple[int, int]], list[str]]:
        """[(char position of '(YYYY)', movie_id)] in order, plus unresolvable 'Title (Year)' strings.

        For each '(YYYY)', try the words just before it, longest suffix first (up to 20 words, stopping at
        quotes/markdown/line breaks), and accept an exact title match for that year.
        """
        found, unknown = [], []
        for m in YEAR_PAREN.finditer(text):
            year = int(m.group(1))
            before = re.split(r"[\n\"“”*]", text[max(0, m.start() - 160):m.start()])[-1]
            words = before.strip().split()
            hit = None
            for n in range(min(20, len(words)), 0, -1):     # "Dr. Strangelove or: How I Learned ... Bomb" = 13 words
                key = (_normalize(" ".join(words[-n:])), year)
                if key in self.index:
                    hit = self.index[key]
                    break
            if hit is not None:
                found.append((m.start(), hit))
            elif words and not words[-1].replace(".", "").isdigit() and \
                    not re.search(r"user|rated|ratings|average|gave", before, re.I):
                # report just the title-like tail: "…avg 4.07, or The Matrix" -> "The Matrix"
                tail = re.split(r"[,;:()]|\b(?:or|and|like|watch|try)\b", before)[-1].split()
                unknown.append(" ".join((tail or words)[-6:]) + f" ({year})")
        return found, unknown

    def mentioned(self, text: str) -> tuple[list[int], list[str]]:
        found, unknown = self.mentions(text)
        return list(dict.fromkeys(m for _, m in found)), unknown


# ------------------------------------------------------------------ number scopes
def _numbers_in(obj, acc: list[float]) -> list[float]:
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.append(float(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            for x, y in re.findall(r"(\d)_(\d)", str(k)):   # threshold in a field name: n_rated_2_5_or_lower
                acc.append(float(f"{x}.{y}"))
            _numbers_in(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, acc)
    return acc


def _scopes(outputs: list, data: MovieData) -> tuple[dict[int, set[float]], set[float]]:
    """Split every number in the tool outputs into per-movie scopes (a dict describing movie m, including
    everything nested in it) and 'free' numbers that belong to no movie (profile averages, genre shares)."""
    by_title = {data.label(int(m)): int(m) for m in data.movies.index}
    known = set(by_title.values())
    movie_vals: dict[int, set[float]] = {}
    free: set[float] = set()

    def movie_of(d: dict):
        if isinstance(d.get("movie_id"), int) and d["movie_id"] in known:   # never crash on a stray id
            return d["movie_id"]
        for key in ("title", "movie"):
            if isinstance(d.get(key), str) and d[key] in by_title:
                return by_title[d[key]]
        return None

    def walk(obj, owners: tuple):
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            if owners:
                for m in owners:
                    movie_vals.setdefault(m, set()).add(float(obj))
            else:
                free.add(float(obj))
        elif isinstance(obj, dict):
            m = movie_of(obj)
            own = owners + ((m,) if m is not None else ())
            for k, v in obj.items():
                if k == "movie_id":
                    continue
                for a, b in re.findall(r"(\d)_(\d)", k):      # "n_rated_2_5_or_lower" states a 2.5 threshold
                    free.add(float(f"{a}.{b}"))
                for d in re.findall(r"_(\d)_", k):
                    free.add(float(d))
                walk(v, own)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, owners)

    for o in outputs:
        walk(o, ())
    return movie_vals, free


def ungrounded_numbers(answer: str, outputs: list) -> list[str]:
    """Tolerance 0.05 lets the model round (4.45 -> 4.5); a value x100 is accepted too (0.024 -> '2.4%')."""
    values = sorted({v for o in outputs for v in _numbers_in(o, [])})
    return [tok for tok in DECIMAL.findall(answer) if not _close(float(tok), values)]


def _blocks(text: str) -> list[tuple[int, int]]:
    """Paragraphs and *top-level* list items: the unit within which a recommendation and its evidence are written.
    Indented sub-bullets ("2. **Metropolis (1927)**" then "   - similar users rated it 3.89") belong to their parent
    item; splitting them off lost the subject and produced false 'misattributed' flags in the memory suite."""
    bounds = [0] + [m.start() for m in re.finditer(r"\n[ \t]*\n(?=\S)|\n(?:[-*•]|\d+[.)])\s", text)] + [len(text)]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def _is_subject(text: str, pos: int) -> bool:
    """The movie a block is about: a bold title that *opens* its line or list item ("1. **Shutter Island (2010)**"),
    or a heading. A bold title mid-sentence is evidence, not the subject: "You rated **Minority Report (2002)** 4
    stars, and similar users rated this movie 4.08" is about the list item's movie (false positive in the memory run)."""
    line_start = text.rfind("\n", 0, pos) + 1
    prefix = text[line_start:pos].lstrip()
    if prefix.startswith("#"):
        return True
    opens_line = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)?\*\*", prefix) is not None
    return opens_line and "**" in text[pos:pos + 10]


def misattributed_numbers(answer: str, outputs: list, titles: TitleIndex) -> list[str]:
    """Numbers are attributed to the *subject* movie of their block (the bold/heading title the paragraph or list
    item is about), not to whichever title happens to precede them: in "**Fight Club (1999)** - you rated *Star Wars*
    5 stars and similar users gave it 4.9", 4.9 is about Fight Club. A number passes if it is in the scope of the
    subject, of any movie named in the block, or free; it is flagged only if it belongs to other movies alone."""
    found, _ = titles.mentions(answer)
    if not found:
        return []
    movie_vals, free = _scopes(outputs, titles.data)
    issues = []
    for a, b in _blocks(answer):
        in_block = [(pos, mid) for pos, mid in found if a <= pos < b]
        if not in_block:
            continue
        subjects = [(pos, mid) for pos, mid in in_block if _is_subject(answer, pos)]
        block_movies = {mid for _, mid in in_block}
        for m in DECIMAL.finditer(answer, a, b):
            x = float(m.group(1))
            prior_subjects = [mid for pos, mid in subjects if pos < m.start()]
            subject = prior_subjects[-1] if prior_subjects else None
            ok_scope = set(movie_vals.get(subject, ())) if subject else set()
            if subject is None:                       # no bold title: any movie named in the block will do
                for mid in block_movies:
                    ok_scope |= movie_vals.get(mid, set())
            if _close(x, ok_scope) or _close(x, free):
                continue
            others = [o for o, vals in movie_vals.items() if o not in ({subject} if subject else block_movies)
                      and _close(x, vals)]
            if others:
                about = titles.data.label(subject) if subject else "the movies in this passage"
                issues.append(f"{m.group(1)} stated about {about} (the tools reported it for "
                              f"{titles.data.label(others[0])})")
    return issues


def wrong_user_ratings(answer: str, titles: TitleIndex, user_id: int | None) -> list[str]:
    """Check 'you rated X N stars' against the user's real rating in the dataset."""
    if user_id is None or user_id not in titles.data.user_ratings:
        return []
    ur = titles.data.user_ratings[user_id]
    found, _ = titles.mentions(answer)
    issues = []
    for m in USER_RATING_CLAIM.finditer(answer):
        if not (m.group("unit") or m.group("art")):        # a bare number is not a rating claim
            continue
        if OTHER_RATER.search(m.group("inner")):            # "...you rated X, and similar users gave it 4.5"
            continue
        claimed = float(m.group("n"))
        movies = [mid for pos, mid in found if m.start("inner") <= pos < m.start("n")]
        if not movies:                               # "you gave it a 3" -> the movie named just before
            prev = [mid for pos, mid in found if pos < m.start()]
            movies = prev[-1:] if re.search(r"\bit\b", m.group("inner")) else []
        for mid in movies:
            actual = ur.get(mid)
            if actual is None:
                issues.append(f"claims the user rated {titles.data.label(mid)} {m.group('n')}, but they never rated it")
            elif abs(actual - claimed) > 0.01:
                issues.append(f"claims the user rated {titles.data.label(mid)} {m.group('n')}, actual rating {actual:g}")
    return issues


def check_answer(answer: str, outputs: list, titles: TitleIndex, user_id: int | None = None) -> dict:
    """outputs: every tool output (dicts) seen so far in the conversation."""
    mentioned, unknown = titles.mentioned(answer)
    blob = json.dumps(outputs, ensure_ascii=False)
    ungrounded = [titles.data.label(m) for m in mentioned
                  if f'"movie_id": {m},' not in blob and titles.data.label(m) not in blob]
    return {"n_titles": len(mentioned), "n_decimals": len(DECIMAL.findall(answer)),
            "n_rating_claims": len(USER_RATING_CLAIM.findall(answer)),
            "hallucinated_titles": unknown, "ungrounded_titles": ungrounded,
            "ungrounded_numbers": ungrounded_numbers(answer, outputs),
            "misattributed_numbers": misattributed_numbers(answer, outputs, titles),
            "wrong_user_ratings": wrong_user_ratings(answer, titles, user_id)}


ISSUE_KEYS = ("hallucinated_titles", "ungrounded_titles", "ungrounded_numbers", "misattributed_numbers",
              "wrong_user_ratings")


def has_issues(report: dict) -> bool:
    return any(report.get(k) for k in ISSUE_KEYS)


def revision_request(report: dict) -> str:
    labels = {"hallucinated_titles": "titles that are not in the dataset",
              "ungrounded_titles": "movies no tool returned",
              "ungrounded_numbers": "numbers not found in any tool output",
              "misattributed_numbers": "numbers attached to the wrong movie",
              "wrong_user_ratings": "incorrect statements about the user's own ratings"}
    parts = [f"{labels[k]}: " + "; ".join(report[k]) for k in ISSUE_KEYS if report.get(k)]
    return ("[automatic grounding check] Your answer contains " + " | ".join(parts) +
            ". Rewrite the answer so every movie and number comes from the tool outputs (call a tool if you need "
            "more evidence). Do not mention this check to the user.")
