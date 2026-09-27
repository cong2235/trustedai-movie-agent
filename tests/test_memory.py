"""Deterministic tests for short-term (conversation compaction) and long-term (per-user store) memory."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.memory import MemoryStore, NullMemory  # noqa: E402
from movie_agent.recommender import Recommender  # noqa: E402
from movie_agent.telemetry import Telemetry  # noqa: E402
from movie_agent.tools import MovieTools, ToolError  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return MovieData.load()


@pytest.fixture(scope="module")
def cf(data):
    return CFModel(data)


def make_tools(data, cf, memory=None):
    return MovieTools(
        data=data,
        cf=cf,
        content=None,
        rec=Recommender(data, cf, None),
        memory=memory if memory is not None else MemoryStore(":memory:"),
    )


# ------------------------------------------------------------------ long-term store
def test_users_are_isolated():
    m = MemoryStore(":memory:")
    m.add(30, "dismissed", movie_id=2959)
    m.add(30, "avoid_genre", note="War")
    assert m.list(15) == [] and m.excluded_movie_ids(15) == set() and m.avoided_genres(15) == []
    mem_id = m.list(30)[0]["memory_id"]
    assert m.forget(15, mem_id) is False  # another user cannot delete it
    assert len(m.list(30)) == 2


def test_duplicate_remember_is_idempotent():
    m = MemoryStore(":memory:")
    a = m.add(1, "seen", movie_id=356)
    b = m.add(1, "seen", movie_id=356)
    assert a == b and len(m.list(1)) == 1


def test_duplicates_in_an_old_store_are_cleaned_on_open(tmp_path):
    """Stores written by the first version could hold duplicates; opening them must dedupe and add the index."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE memories (memory_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,"
        " kind TEXT NOT NULL, movie_id INTEGER, note TEXT, created_ts REAL NOT NULL);"
    )
    con.executemany(
        "INSERT INTO memories (user_id, kind, movie_id, note, created_ts) VALUES (?,?,?,?,?)",
        [(1, "seen", 356, None, 0.0), (1, "seen", 356, None, 1.0), (1, "avoid_genre", None, "War", 2.0)],
    )
    con.commit()
    con.close()
    m = MemoryStore(path)
    assert len(m.list(1)) == 2
    m.add(1, "seen", movie_id=356)
    assert len(m.list(1)) == 2


def test_memory_survives_a_restart(tmp_path):
    path = tmp_path / "mem.db"
    MemoryStore(path).add(30, "seen", movie_id=356)
    reopened = MemoryStore(path)  # new process / container restart
    assert reopened.excluded_movie_ids(30) == {356}


def test_concurrent_writes_from_many_sessions(tmp_path):
    m = MemoryStore(tmp_path / "mem.db")
    errors = []

    def writer(u):
        try:
            for i in range(50):
                m.add(u, "preference", note=f"note {i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(u,)) for u in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sum(len(m.list(u)) for u in range(8)) == 400


def test_invalid_memories_are_rejected(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(1)
    for kind, kw in [("avoid_genre", {"note": "romcom"}), ("seen", {}), ("loves", {"note": "x"})]:
        with pytest.raises(ToolError):
            tools.remember(kind, **kw)
    assert tools.remember("avoid_genre", note="  sci-fi ")["note"] == "Sci-Fi"  # canonicalised


def test_ambiguous_movie_is_not_stored(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(1)
    with pytest.raises(ToolError):
        tools.remember("seen", movie="Star Wars")  # 6 films: must ask, not guess
    assert tools.memory.list(1) == []


def test_every_excluding_kind_filters_recommendations(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(30)
    first = [r["movie_id"] for r in tools.recommend_movies(n=8)["recommendations"]]
    for kind, mid in zip(("seen", "dismissed", "disliked", "liked"), first):
        tools.remember(kind, movie=mid)
    tools.set_user(30)
    again = {r["movie_id"] for r in tools.recommend_movies(n=8)["recommendations"]}
    assert not again & set(first[:4])


def test_more_like_never_returns_the_anchor(data, cf):
    """Regression: 'something like The Machinist' returned The Machinist when the user had not rated it."""
    tools = make_tools(data, cf)
    for user, anchor in [(15, "The Machinist"), (15, "Shutter Island"), (30, "Toy Story"), (30, "Inception")]:
        tools.set_user(user)
        aid = tools.resolve(anchor)
        out = tools.recommend_movies(n=10, more_like=[anchor])
        assert aid not in {r["movie_id"] for r in out["recommendations"]}


def test_memory_disabled_is_harmless(data, cf):
    tools = make_tools(data, cf, memory=NullMemory())
    tools.set_user(30)
    assert tools.remember("seen", movie="Forrest Gump")["ok"]
    assert tools.memory_context() == "" and tools.recommend_movies(n=3)["recommendations"]


def test_memory_appears_in_model_context(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(30)
    tools.remember("seen", movie="Forrest Gump")
    tools.remember("avoid_genre", note="War")
    tools.set_user(30)
    ctx = tools.memory_context()
    assert "Forrest Gump (1994)" in ctx and "War" in ctx and "auto-excluded" in ctx
    assert "Profile:" in tools.session.profile_summary


# ------------------------------------------------------------------ short-term: compaction invariants
class _ToolCallingBackend:
    """OpenAI-shaped messages: each turn = one tool call, its result, then an answer."""

    def __init__(self):
        self.n = 0

    def step(self, system, messages, on_token=None):
        last = messages[-1]
        if last["role"] == "user":  # first step of a turn: call a tool
            self.n += 1
            cid = f"call_{self.n}"
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": cid, "type": "function", "function": {"name": "get_user_profile", "arguments": "{}"}}
                    ],
                }
            )
            return "", [(cid, "get_user_profile", {})], "tool_use", {"input_tokens": 1, "output_tokens": 1}
        text = f"answer {self.n}"
        messages.append({"role": "assistant", "content": text})
        return text, [], "stop", {"input_tokens": 1, "output_tokens": 1}

    def tool_results(self, messages, results):
        for cid, text, _ in results:
            messages.append({"role": "tool", "tool_call_id": cid, "content": text})

    def user(self, messages, text):
        messages.append({"role": "user", "content": text})

    def assistant(self, messages, text):
        messages.append({"role": "assistant", "content": text})


def _check_pairing(messages):
    """What the OpenAI API enforces: every tool message answers a preceding assistant tool call, and every
    assistant tool call is answered. A violation is a 400 error in the middle of a conversation."""
    open_calls = set()
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            assert not open_calls, "new tool calls before previous ones were answered"
            open_calls = {c["id"] for c in m["tool_calls"]}
        elif m["role"] == "tool":
            assert m["tool_call_id"] in open_calls, "orphan tool message"
            open_calls.discard(m["tool_call_id"])
    assert not open_calls, "unanswered tool call"


def test_compaction_keeps_tool_pairing_and_bounds_context(data, cf, tmp_path, monkeypatch):
    from movie_agent import config
    from movie_agent.agent import MovieAgent

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tools = make_tools(data, cf)
    agent = MovieAgent(
        tools=tools,
        provider="openai",
        model="gpt-4o-mini",
        telemetry=Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl"),
    )
    agent.backend = _ToolCallingBackend()
    agent.start(15)
    sizes = []
    for i in range(8):
        agent.ask(f"question {i}")
        _check_pairing(agent.messages)
        sizes.append(len(agent.messages))
    keep = config.KEEP_FULL_TURNS
    # the model always sees the last `keep` turns in full plus the current one (4 messages each: q, call, result,
    # answer); every older turn is 2 messages (q, answer)
    assert sizes[-1] == 2 * (8 - keep - 1) + 4 * (keep + 1)
    assert sizes[-1] == sizes[-2] + 2  # grows by 2 per turn, not by 4 + tool output
    questions = [m["content"] for m in agent.messages if m["role"] == "user"]
    assert questions == [f"question {i}" for i in range(8)]  # nothing the user said is lost
    answers = [m["content"] for m in agent.messages if m["role"] == "assistant" and not m.get("tool_calls")]
    assert answers == [f"answer {i + 1}" for i in range(8)]  # old answers survive as the turn summary


def test_new_session_resets_short_term_but_not_long_term(data, cf, tmp_path, monkeypatch):
    from movie_agent.agent import MovieAgent

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tools = make_tools(data, cf)
    agent = MovieAgent(
        tools=tools,
        provider="openai",
        model="gpt-4o-mini",
        telemetry=Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl"),
    )
    agent.backend = _ToolCallingBackend()
    agent.start(30)
    agent.ask("hello")
    tools.remember("seen", movie="Forrest Gump")
    tools.recommend_movies(n=3)
    assert tools.session.suggested
    agent.start(30)
    assert agent.messages == [] and agent._turns == [] and tools.session.suggested == []
    assert "Forrest Gump" in agent._context()


def test_blind_spot_entry_points_name_whose_average(data, cf):
    """Regression (found by scripts/audit_answers.py): the model reported a 2-user neighbourhood average as the
    movie's overall average. The tool now returns both, explicitly named."""
    tools = make_tools(data, cf)
    tools.set_user(1)
    eps = [e for b in tools.genre_blind_spots()["blind_spots"] for e in b["entry_points_liked_by_similar_users"]]
    assert eps
    for e in eps:
        mid = tools.resolve(e["title"])
        assert e["avg_rating_all_users"] == round(float(data.movie_stats.loc[mid, "mean"]), 2)
        assert {"avg_among_your_similar_users", "n_similar_users_who_rated_it", "n_ratings_all_users"} <= set(e)


def test_model_cannot_ask_for_repeats():
    """Regression: for "give me three more" gpt-4o-mini set allow_repeats=true in 3/3 runs and re-suggested movies.
    The flag is no longer offered to the model."""
    from movie_agent.tools import TOOL_SCHEMAS

    props = next(t for t in TOOL_SCHEMAS if t["name"] == "recommend_movies")["input_schema"]["properties"]
    assert "allow_repeats" not in props
    assert "'after 2000' -> 2001" in props["min_year"]["description"]


def test_long_titles_are_recognised_by_the_guardrail(data):
    from movie_agent.guardrails import TitleIndex

    ids, unknown = TitleIndex(data).mentioned(
        "Try **Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb (1964)**."
    )
    assert ids == [750] and unknown == []  # was reported as a hallucinated "Love the Bomb (1964)"


# ---- fixes from the 3x memory run (lt_forget_preference 0/3, lt_one_off_not_stored 0/3, lt_override 1/3)
@pytest.mark.parametrize(
    "msg,one_off",
    [
        ("Just for tonight I'm not in the mood for comedies - what should I watch?", True),
        ("No horror this time please", True),
        ("Please remember that I don't like horror movies.", False),
        ("I never want war movies recommended to me. Please remember that.", False),
        ("Today I'm curious though - recommend me a war movie, just this once.", True),
        ("I hate musicals, not tonight or ever", False),
    ],
)
def test_one_off_detection(msg, one_off):
    from movie_agent.tools import _is_one_off

    assert _is_one_off(msg) is one_off


def test_one_off_constraint_is_not_stored(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(1)
    tools.session.last_user_message = "Just for tonight I'm not in the mood for comedies"
    with pytest.raises(ToolError):
        tools.remember("avoid_genre", note="Comedy")
    assert tools.memory.list(1) == []


def test_retraction_cannot_restore_the_preference(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(30)
    tools.session.last_user_message = "Please remember that I don't like horror movies."
    assert tools.remember("avoid_genre", note="I don't like horror movies")["note"] == "Horror"  # genre extracted
    tools.session.last_user_message = "Actually I've changed my mind - I'm fine with horror movies now. Forget that."
    with pytest.raises(ToolError):
        tools.remember("avoid_genre", note="Horror")
    out = tools.forget_memory(kind="avoid_genre", genre="horror movies")  # by content, no id
    assert out["ok"] and out["removed"] == [{"kind": "avoid_genre", "what": "Horror"}]
    assert tools.memory.avoided_genres(30) == []


def test_explicit_include_beats_remembered_and_explicit_exclude(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(30)
    tools.remember("avoid_genre", note="War")
    out = tools.recommend_movies(n=3, include_genres=["War"], exclude_genres=["War"])  # what the model sent
    assert out["recommendations"] and all("War" in r["genres"] for r in out["recommendations"])
    assert tools.memory.avoided_genres(30) == ["War"]  # memory kept


def test_string_instead_of_list_is_coerced(data, cf):
    """Regression: more_like="The Machinist (2004)" (string) was iterated character by character."""
    tools = make_tools(data, cf)
    tools.set_user(15)
    text, is_error = tools.call(
        "recommend_movies", {"more_like": "The Machinist (2004)", "n": "3", "exclude_genres": "Animation"}
    )
    import json as _json

    out = _json.loads(text)
    assert not is_error and out["applied_constraints"]["more_like"] == ["The Machinist (2004)"]
    assert len(out["recommendations"]) == 3
    assert all("Animation" not in r["genres"] for r in out["recommendations"])


def test_independent_audit_catches_real_errors_only(tmp_path, data):
    """scripts/audit_answers.py re-derives facts from ratings.csv. Built from real transcript sentences:
    one true error (neighbourhood average presented as the movie's average) and three former false positives."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import audit_answers

    from movie_agent.guardrails import TitleIndex

    md = (
        "# case (user 15)\n\n**User:** q\n\n**Assistant:**\n\n"
        "1. **Shutter Island (2010)**  \n   - Average Rating: 4.02  \n   - You rated *Alien (1979)* 5 stars, and "
        "similar users average 4.08 stars for this film.\n\n"
        "2. **The Usual Suspects (1995)** - It has a high average rating of 4.24, and users who liked "
        "**The Shawshank Redemption (1994)** also appreciated this film.\n\n"
        "3. **Drive (2011)** - It has a solid average rating of 3.77 from 91 ratings.\n\n"
        "- *It's a Wonderful Life (1946)* - Average rating of 5.0\n\n> PASS\n"
    )
    f = tmp_path / "case.md"
    f.write_text(md, encoding="utf-8")
    issues, n = audit_answers.audit_file(f, data, TitleIndex.for_data(data))
    details = " | ".join(i["detail"] for i in issues)
    assert "It's a Wonderful Life" in details and "dataset mean 4.03" in details  # the real error
    for ok in ("Shutter Island", "The Usual Suspects", "The Shawshank", "Alien"):
        assert ok not in details  # former false positives
    count_issue = [i for i in issues if "Drive" in i["detail"]]
    assert count_issue == [] or int(data.movie_stats.loc[data.find_movie("Drive 2011")[0]["movie_id"], "count"]) != 91


def test_remember_this_request_scope_is_not_stored(data, cf):
    tools = make_tools(data, cf)
    tools.set_user(1)
    with pytest.raises(ToolError, match="Nothing stored"):
        tools.remember("avoid_genre", note="Comedy", scope="this_request")
    assert not tools.memory.avoided_genres(tools.session.user_id)
