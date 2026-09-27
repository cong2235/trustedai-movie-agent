"""Offline movie attributes: loading, matching, violence filter and their use by the tools (no API calls)."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.attributes import MOODS, MovieAttributes  # noqa: E402
from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.memory import MemoryStore  # noqa: E402
from movie_agent.recommender import Recommender  # noqa: E402
from movie_agent.tools import MovieTools, ToolError  # noqa: E402

TOY_STORY, HEAT, USUAL_SUSPECTS = 1, 6, 50


@pytest.fixture(scope="module")
def data():
    return MovieData.load()


@pytest.fixture(scope="module")
def cf(data):
    return CFModel(data)


@pytest.fixture(scope="module")
def attrs(data, tmp_path_factory):
    path = tmp_path_factory.mktemp("attrs") / "movie_attributes.jsonl"
    rows = [
        {"movie_id": TOY_STORY, "moods": ["family-friendly", "funny"], "twist": 0, "violence": 0},
        {"movie_id": HEAT, "moods": ["tense", "action-packed"], "twist": 1, "violence": 3},
        {"movie_id": USUAL_SUSPECTS, "moods": ["tense", "dark"], "twist": 3, "violence": 2},
        {"movie_id": 999999, "moods": ["funny"], "twist": 0, "violence": 0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return MovieAttributes.load(data, path)


def test_load_and_card(data, attrs):
    row = {int(m): i for i, m in enumerate(data.movies.index)}
    assert attrs.known.sum() == 3
    assert attrs.card(row[USUAL_SUSPECTS]) == {"moods": ["dark", "tense"], "twist_0_3": 3, "violence_0_3": 2}
    assert attrs.card(row[2]) is None


def test_match_is_neutral_for_unknown_movies(data, attrs):
    row = {int(m): i for i, m in enumerate(data.movies.index)}
    m = attrs.match(["tense"], twist_ending=True)
    assert m[row[USUAL_SUSPECTS]] == pytest.approx(1.0)
    assert m[row[HEAT]] == pytest.approx((1 + 1 / 3) / 2)
    assert m[row[TOY_STORY]] == 0.0
    assert m[row[2]] == pytest.approx(np.mean([1.0, (1 + 1 / 3) / 2, 0.0]))


def test_unknown_mood_is_rejected(attrs):
    with pytest.raises(ValueError):
        attrs.match(["hilarious"])
    assert "hilarious" not in MOODS


def test_violence_filter_keeps_unknown(data, attrs):
    row = {int(m): i for i, m in enumerate(data.movies.index)}
    ok = attrs.violence_ok(1)
    assert not ok[row[HEAT]] and not ok[row[USUAL_SUSPECTS]] and ok[row[TOY_STORY]]
    assert ok[row[2]]
    assert attrs.violence_ok(None).all()


def test_recommend_applies_violence_and_reports_attributes(data, cf, attrs):
    tools = MovieTools(
        data=data,
        cf=cf,
        content=None,
        rec=Recommender(data, cf, None),
        memory=MemoryStore(":memory:"),
        attributes=attrs,
    )
    tools.set_user(15)
    out = tools.recommend_movies(n=10, moods=["tense"], max_violence=1)
    ids = [r["movie_id"] for r in out["recommendations"]]
    assert HEAT not in ids and USUAL_SUSPECTS not in ids
    assert out["applied_constraints"]["max_violence"] == 1


def test_attribute_request_without_attributes_is_an_error(data, cf):
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    tools.set_user(15)
    with pytest.raises(ToolError, match="not available"):
        tools.recommend_movies(n=3, moods=["funny"])
    text, is_error = tools.call("recommend_movies", {"n": 3, "moods": ["not-a-mood"]})
    assert is_error
