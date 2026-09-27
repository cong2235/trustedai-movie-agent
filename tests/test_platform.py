"""Tests for the production layer: guardrails, telemetry/monitor, re-ranker fallback, graph model."""

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import monitor  # noqa: E402
from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.graph import RP3Beta  # noqa: E402
from movie_agent.guardrails import TitleIndex, check_answer, has_issues, ungrounded_numbers  # noqa: E402
from movie_agent.rerank import LLMRerank, NoRerank  # noqa: E402
from movie_agent.telemetry import Telemetry, cost_usd  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return MovieData.load()


@pytest.fixture(scope="module")
def titles(data):
    return TitleIndex(data)


def test_guardrail_flags_absent_title_and_ungrounded_movie(titles):
    outputs = [{"recommendations": [{"movie_id": 79132, "title": "Inception (2010)", "avg_rating": 4.07}]}]
    rep = check_answer("Try **Inception (2010)**, **The Matrix (1999)** or Alien (1979).", outputs, titles)
    assert rep["hallucinated_titles"] == ["The Matrix (1999)"]
    assert rep["ungrounded_titles"] == ["Alien (1979)"]  # real movie, but no tool returned it
    assert has_issues(rep)


def test_guardrail_handles_tricky_titles(titles):
    ids, unknown = titles.mentioned('You rated "2001: A Space Odyssey (1968)" and A.I. Artificial Intelligence (2001).')
    assert len(ids) == 2 and not unknown


def test_numeric_grounding_tolerance():
    outs = [{"avg": 4.45, "share": 0.024, "nested": [{"x": 3.86}]}]
    assert ungrounded_numbers("avg 4.5, 2.4% share, 3.9 in drama", outs) == []  # rounding and x100 are fine
    assert ungrounded_numbers("they average 4.9", outs) == ["4.9"]


def test_clean_answer_passes(titles):
    outputs = [{"results": [{"movie_id": 1200, "title": "Aliens (1986)", "avg_rating": 3.97}]}]
    assert not has_issues(check_answer("**Aliens (1986)** averages 3.97.", outputs, titles))


def test_telemetry_roundtrip_and_kpis(tmp_path):
    tel = Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl")
    for i, (lat, issue) in enumerate([(3000, 0), (5000, 1), (40000, 0)]):
        tel.log_turn(
            {
                "turn_id": f"t{i}",
                "session_id": "s",
                "user_id": 1,
                "provider": "openai",
                "model": "gpt-4o-mini",
                "question": "q",
                "answer": "a",
                "latency_ms": lat,
                "llm_calls": 2,
                "input_tokens": 1000,
                "output_tokens": 100,
                "cost_usd": cost_usd("gpt-4o-mini", 1000, 100),
                "n_tool_calls": 1,
                "n_tool_errors": 0,
                "stop_reason": "stop",
                "guardrail_issues": issue,
                "guardrail_detail": "{}",
                "revised": issue,
                "issues_after_revision": 0 if issue else None,
                "ts": time.time(),
            },
            [
                {
                    "tool": "search_movies",
                    "input": {"query": "x"},
                    "ms": 50,
                    "is_error": False,
                    "output_chars": 10,
                    "output": {"reranker": {"kind": "llm", "ms": 900}},
                }
            ],
        )
    tel.log_feedback("t0", 1)
    turns, calls = monitor.load(tmp_path / "t.db")
    k = monitor.kpis(turns, calls)
    assert k["turns"] == 3 and k["guardrail_trigger_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert k["revision_fix_rate"] == 1.0 and k["thumbs_up_rate"] == 1.0 and k["rerank_calls"] == 3
    assert any("latency_p95_s" in a for a in monitor.alerts(k))  # the 40 s turn breaches the p95 SLO
    assert (tmp_path / "a.jsonl").read_text().count('"event": "tool_call"') == 3
    later, later_calls = monitor.load(tmp_path / "t.db", since_ts=time.time() + 60)  # parameterised time filter
    assert later.empty and later_calls.empty


def test_reranker_failure_keeps_first_stage_order(data):
    ids = [1, 2, 3, 296]
    first = [0.1, 0.9, 0.5, 0.3]
    assert NoRerank().rerank("q", ids, first, data).order == [2, 3, 296, 1]
    res = LLMRerank()._finish(ids, first, {}, time.time(), error="boom")  # what a failed LLM call returns
    assert res.order == [2, 3, 296, 1] and res.error == "boom"


def test_rp3beta_graph(data):
    cf = CFModel(data)
    g = RP3Beta(cf, kg_weight=1.0).fit()
    s = g.scores(15)
    assert s.shape == (len(cf.movie_ids),) and np.isfinite(s).all()
    best = int(cf.movie_ids[np.argmax(np.where(cf.rated_mask(15), -1, s))])
    assert g.paths(15, best)  # explainable as graph paths


class _ScriptedBackend:
    """Stands in for the LLM: calls one tool, then hallucinates, then answers correctly after the revision request."""

    def __init__(self):
        self.replies = [
            ("", [("c1", "get_movie_details", {"movie": "Inception"})]),
            ("Watch Inception (2010), avg 4.07, or The Matrix (1999), avg 4.9.", []),
            ("Watch Inception (2010), avg 4.07.", []),
        ]
        self.seen_revision_request = False

    def step(self, system, messages, on_token=None):
        text, calls = self.replies.pop(0)
        messages.append({"role": "assistant", "content": text})
        return text, calls, ("tool_use" if calls else "stop"), {"input_tokens": 100, "output_tokens": 10}

    def tool_results(self, messages, results):
        messages.extend({"role": "tool", "content": out} for _, out, _ in results)

    def user(self, messages, text):
        self.seen_revision_request |= text.startswith("[automatic grounding check]")
        messages.append({"role": "user", "content": text})

    def assistant(self, messages, text):
        messages.append({"role": "assistant", "content": text})


def test_online_guardrail_revises_and_logs(tmp_path, data, monkeypatch):
    from movie_agent.agent import MovieAgent
    from movie_agent.recommender import Recommender
    from movie_agent.tools import MovieTools

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # client is constructed but never called
    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None))
    tel = Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl")
    agent = MovieAgent(tools=tools, provider="openai", model="gpt-4o-mini", telemetry=tel)
    agent.backend = _ScriptedBackend()
    agent.start(15)
    res = agent.ask("anything good?")
    assert res.revised and agent.backend.seen_revision_request
    assert res.guardrail["first"]["hallucinated_titles"] == ["The Matrix (1999)"]
    assert res.guardrail["first"]["ungrounded_numbers"] == ["4.9"]
    assert not has_issues(res.guardrail["after_revision"]) and "Matrix" not in res.text
    turns, _ = monitor.load(tmp_path / "t.db")
    row = turns.iloc[0]
    assert row["revised"] == 1 and row["issues_after_revision"] == 0 and row["llm_calls"] == 3


def test_long_term_memory_excludes_seen_movies_across_sessions(data):
    from movie_agent.memory import MemoryStore
    from movie_agent.recommender import Recommender
    from movie_agent.tools import MovieTools

    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    tools.set_user(30)
    first = [r["movie_id"] for r in tools.recommend_movies(n=5)["recommendations"]]
    for m in first[:2]:
        assert tools.remember("seen", movie=m)["ok"]
    tools.remember("preference", note="doesn't like war movies")
    tools.set_user(30)  # new session: session state resets, memory stays
    again = [r["movie_id"] for r in tools.recommend_movies(n=5)["recommendations"]]
    assert not set(first[:2]) & set(again)
    assert "doesn't like war movies" in tools.memory_context()
    assert "seen:" in tools.memory_context()
    mem_id = tools.list_memories()["memories"][0]["memory_id"]
    assert tools.forget_memory(mem_id)["ok"]
    assert len(tools.list_memories()["memories"]) == 2


def test_remember_validates_input(data):
    from movie_agent.memory import MemoryStore
    from movie_agent.recommender import Recommender
    from movie_agent.tools import MovieTools, ToolError

    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    tools.set_user(1)
    with pytest.raises(ToolError):
        tools.remember("seen")  # needs a movie
    with pytest.raises(ToolError):
        tools.remember("loves")  # unknown kind


class _EchoBackend:
    """Answers every question immediately with a long text, to exercise history compaction."""

    def step(self, system, messages, on_token=None):
        text = "answer " + "x" * 50
        if on_token:
            for part in (text[:10], text[10:]):
                on_token(part)
        messages.append({"role": "assistant", "content": text})
        return text, [], "stop", {"input_tokens": 1, "output_tokens": 1}

    def tool_results(self, messages, results):
        pass

    def user(self, messages, text):
        messages.append({"role": "user", "content": text})

    def assistant(self, messages, text):
        messages.append({"role": "assistant", "content": text})


def test_history_compaction_and_streaming(tmp_path, data, monkeypatch):
    from movie_agent import config
    from movie_agent.agent import MovieAgent
    from movie_agent.memory import MemoryStore
    from movie_agent.recommender import Recommender
    from movie_agent.tools import MovieTools

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    agent = MovieAgent(
        tools=tools,
        provider="openai",
        model="gpt-4o-mini",
        telemetry=Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl"),
    )
    agent.backend = _EchoBackend()
    agent.start(15)
    # simulate an earlier turn that carried a bulky tool exchange
    agent.messages += [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": "", "tool_calls": ["..."]},
        {"role": "tool", "content": "{" + "big tool output " * 200 + "}"},
        {"role": "assistant", "content": "a0"},
    ]
    agent._turns.append({"start": 0, "question": "q0", "answer": "a0"})
    streamed = []
    for i in range(1, 5):
        res = agent.ask(f"q{i}", on_token=streamed.append)
    assert res.ttft_s is not None and "".join(streamed[-2:]).startswith("answer")
    assert not any(m.get("role") == "tool" for m in agent.messages)  # old tool output was dropped
    assert len(agent.messages) == 2 * (len(agent._turns))  # every turn now: question + answer
    assert [m["content"] for m in agent.messages if m["role"] == "user"][:2] == ["q0", "q1"]
    assert config.KEEP_FULL_TURNS == 2


# ---- guardrail v2: real false positives from the 3x LLM run must stay clean; true errors must still be caught
def _out(data, *titles_with_values):
    """Minimal tool output: [{movie_id, title, values...}], optionally nesting evidence movies."""
    res = []
    for q, vals, nested in titles_with_values:
        mid = data.find_movie(q)[0]["movie_id"]
        d = {"movie_id": mid, "title": data.label(mid), **vals}
        if nested:
            d["because_you_rated"] = [
                {"title": data.label(data.find_movie(t)[0]["movie_id"]), "your_rating": r} for t, r in nested
            ]
        res.append(d)
    return [{"recommendations": res}]


@pytest.mark.parametrize(
    "user,text",
    [
        (
            15,
            "Similar users rate **Inception (2010)** highly. You rated it **3.5 stars**. One user with a high "
            "similarity to you rated it **4.5 stars**.",
        ),  # "to you rated" is not a claim
        (15, "You rated *Terminator 2: Judgment Day (1991)* and *Alien (1979)* highly, and similar users liked it."),
        (
            30,
            'You rated "Raiders of the Lost Ark (1981)" and "Star Wars: Episode V - The Empire Strikes Back (1980)" '
            "both 5 stars, and users who liked those also enjoyed this one, with a predicted rating of 4.8 for you.",
        ),
        (
            30,
            'You gave high ratings to "Star Wars: Episode V - The Empire Strikes Back (1980)" and "Star Wars: Episode IV '
            '- A New Hope (1977)," and this film has a similar appeal, with a predicted rating of 4.8 for you.',
        ),
    ],
)
def test_rating_claim_false_positives_stay_clean(titles, user, text):
    from movie_agent.guardrails import wrong_user_ratings

    assert wrong_user_ratings(text, titles, user) == []


@pytest.mark.parametrize(
    "user,text,needle",
    [
        (1, "You rated **Pulp Fiction (1994)** 5 stars.", "actual rating 3"),
        (1, "**The Game (1997)** is great, and you gave it a 4.5.", "actual rating 5"),
        (15, "You rated both *Alien (1979)* and *Big (1988)* 5 stars.", "never rated"),
        (15, "You rated *Toy Story (1995)* 5 stars.", "actual rating 2.5"),
    ],
)
def test_rating_claim_errors_are_caught(titles, user, text, needle):
    from movie_agent.guardrails import wrong_user_ratings

    issues = wrong_user_ratings(text, titles, user)
    assert issues and needle in issues[0]


def test_numbers_attributed_to_the_block_subject(data, titles):
    from movie_agent.guardrails import misattributed_numbers

    out = _out(
        data,
        ("Fight Club", {"avg_rating": 4.9}, [("Star Wars: Episode V", 5.0)]),
        ("Shutter Island", {"avg_rating": 4.02}, None),
        ("The Game 1997", {"avg_rating": 3.7}, None),
    )
    ok = (
        "1. **Fight Club (1999)** - you rated *Star Wars: Episode V - The Empire Strikes Back (1980)* 5 stars, and "
        "similar users gave it 4.9."
    )
    assert misattributed_numbers(ok, out, titles) == []  # was flagged by v1
    swapped = "**Shutter Island (2010)** averages 3.7. **The Game (1997)** averages 4.02."
    assert len(misattributed_numbers(swapped, out, titles)) == 2


def test_threshold_in_field_name_is_grounded():
    assert ungrounded_numbers("Only 2 users rated it 2.5 or lower.", [{"n_rated_2_5_or_lower": 2}]) == []


def test_avoided_genre_is_enforced_by_tools_across_sessions(data):
    """Regression: the model stored "doesn't like war movies" but did not apply it next session (0/3 runs)."""
    from movie_agent.memory import MemoryStore
    from movie_agent.recommender import Recommender
    from movie_agent.tools import MovieTools

    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    tools.set_user(30)
    tools.remember("avoid_genre", note="war")  # case-insensitive, stored as "War"
    tools.set_user(30)  # next session; the model passes no exclude_genres
    out = tools.recommend_movies(n=8)
    assert all("War" not in r["genres"] for r in out["recommendations"])
    assert out["applied_constraints"]["genres_avoided_from_memory"] == ["War"]
    explicit = tools.recommend_movies(n=3, include_genres=["War"])  # an explicit request overrides the memory
    assert explicit["recommendations"] and all("War" in r["genres"] for r in explicit["recommendations"])


class _FakeChunk:
    def __init__(self, text=None, usage=None):
        from types import SimpleNamespace as NS

        self.usage = usage
        self.choices = [] if text is None else [NS(delta=NS(content=text, tool_calls=None), finish_reason=None)]


class _FakeStream:
    def __init__(self, delay, tag):
        self.delay, self.tag, self.closed = delay, tag, False

    def __iter__(self):
        time.sleep(self.delay)  # slow first chunk = the tail we hedge against
        from types import SimpleNamespace as NS

        for part in (f"{self.tag}-a", f"{self.tag}-b"):
            yield _FakeChunk(part)
        yield _FakeChunk(None, usage=NS(prompt_tokens=10, completion_tokens=2))

    def close(self):
        self.closed = True


def _backend_with(delays, monkeypatch):
    from types import SimpleNamespace as NS

    from movie_agent import config
    from movie_agent.agent import OpenAIBackend

    monkeypatch.setattr(config, "HEDGE_AFTER_S", 0.2)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    be = OpenAIBackend("gpt-4o-mini")
    made = []

    def create(**kw):
        st = _FakeStream(delays[len(made)], f"req{len(made)}")
        made.append(st)
        return st

    be.client = be._hedge_client = NS(chat=NS(completions=NS(create=create)))
    return be, made


def test_hedging_uses_the_faster_duplicate(monkeypatch):
    be, made = _backend_with([6.0, 0.0], monkeypatch)  # first request stalls, the hedge is fast
    t0 = time.time()
    text, calls, stop, u = be.step("ctx", [])
    assert text == "req1-areq1-b" and u["call"]["hedged"] and len(made) == 2
    assert time.time() - t0 < 4.0  # did not wait for the 6 s request (loose: CI load)


def test_no_hedge_when_fast(monkeypatch):
    be, made = _backend_with([0.0, 0.0], monkeypatch)
    text, _, _, u = be.step("ctx", [])
    assert text == "req0-areq0-b" and not u["call"]["hedged"] and len(made) == 1
    assert u["input_tokens"] == 10 and u["output_tokens"] == 2
