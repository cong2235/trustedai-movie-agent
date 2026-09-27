"""Fast correctness tests for the deterministic layer (no embeddings or API key required)."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.cf import CFModel, preference_weight  # noqa: E402
from movie_agent.data import MovieData, display_title  # noqa: E402
from movie_agent.evaluation import ranking_metrics, temporal_split  # noqa: E402
from movie_agent.recommender import Recommender  # noqa: E402
from movie_agent.tools import MovieTools, ToolError  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return MovieData.load()


@pytest.fixture(scope="module")
def cf(data):
    return CFModel(data)


@pytest.fixture(scope="module")
def tools(data, cf):
    t = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None))
    t.set_user(15)
    return t


def test_display_title():
    assert display_title("Usual Suspects, The") == "The Usual Suspects"
    assert display_title("City of Lost Children, The (Cité des enfants perdus, La)") == "The City of Lost Children"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("the usual suspects", 50),
        ("Inception", 79132),
        ("pulp fiction", 296),
        ("Aliens", 1200),
        ("alice in wonderland 2010", 74789),
        ("Alice in Wonderland 1951", 1032),
    ],
)
def test_find_movie(data, query, expected):
    assert data.find_movie(query)[0]["movie_id"] == expected


def test_absent_title_is_not_guessed(tools):
    with pytest.raises(ToolError) as e:
        tools.resolve("The Matrix")
    assert e.value.details["closest_titles"]


def test_ambiguous_title_asks(tools):
    with pytest.raises(ToolError):
        tools.resolve("star wars")


def test_user_similarity_symmetric_and_bounded(cf):
    s = cf.user_sim
    assert np.allclose(s, s.T, atol=1e-5)
    assert s.max() <= 1.0 + 1e-5 and s.min() >= -1.0 - 1e-5
    assert np.all(np.diag(s) == 0)


def test_preference_weight():
    assert list(preference_weight(np.array([5.0, 4.0, 3.0, 1.0]))) == [1.0, 0.5, 0.0, -1.0]


def test_recommend_respects_constraints(tools, data):
    out = tools.recommend_movies(n=8, exclude_genres=["Animation", "Children"], min_year=1990)
    seen = set(data.user_ratings[15].index)
    for r in out["recommendations"]:
        assert r["movie_id"] not in seen
        assert not {"Animation", "Children"} & set(r["genres"])
        assert int(r["title"][-5:-1]) >= 1990


def test_no_repeats_within_session(tools):
    tools.set_user(15)
    a = {r["movie_id"] for r in tools.recommend_movies(n=5)["recommendations"]}
    b = {r["movie_id"] for r in tools.recommend_movies(n=5)["recommendations"]}
    assert a and b and not a & b


def test_similar_users_opinion_reports_own_rating(tools):
    tools.set_user(1)
    out = tools.similar_users_opinion("Pulp Fiction")
    assert out["your_rating"] == 3.0
    assert out["similar_users"]["n"] > 0


def test_unknown_user(tools):
    text, is_err = tools.call("get_user_profile", {"user_id": 9999})
    assert is_err and "does not exist" in text


def test_temporal_split_is_per_user_and_ordered(data):
    sp = temporal_split(data.ratings)
    last_train = sp.train.groupby("userId")["timestamp"].max()
    first_test = sp.test.groupby("userId")["timestamp"].min()
    assert (last_train <= first_test.loc[last_train.index]).all()
    assert set(sp.test["userId"]) == set(data.ratings["userId"])


def test_ranking_metrics():
    m = ranking_metrics(np.array([1, 2, 3, 4]), {2, 9}, k=4)
    assert m["P@4"] == 0.25 and m["R@4"] == 0.5 and m["HR@4"] == 1.0 and m["MRR"] == 0.5


def test_no_prediction_for_already_rated_movie(tools):
    """Regression: the LLM once told user 1 'predicted rating 4.6' for Pulp Fiction, which they rated 3.0."""
    tools.set_user(1)
    out = tools.similar_users_opinion("Pulp Fiction")
    assert "predicted_rating_for_you" not in out and out["your_rating"] == 3.0
    assert "predicted_rating_for_you" not in tools.rec.explain(1, 296)


@pytest.mark.parametrize(
    "query,expected_title",
    [
        ("Terminator 2", "Terminator 2: Judgment Day (1991)"),  # was: The Terminator (1984)
        ("Godfather 2", "The Godfather: Part II (1974)"),  # was: The Godfather (1972)
        ("Alien 3", "Alien³ (1992)"),  # was: Alien (1979)
        ("Ocean's 12", "Ocean's Twelve (2004)"),  # was: Twelve Monkeys
        ("Rocky 4", "Rocky IV (1985)"),
        ("Back to the Future 2", "Back to the Future Part II (1989)"),
        ("Toy Story", "Toy Story (1995)"),  # no number -> not the sequel
        ("The Godfather", "The Godfather (1972)"),
        ("21", "21 (2008)"),  # numeric title beats "movie id 21"
        ("2012", "2012 (2009)"),
    ],
)
def test_sequels_and_numeric_titles_resolve_correctly(tools, data, query, expected_title):
    assert data.label(tools.resolve(query)) == expected_title


def test_numeric_string_still_works_as_movie_id(tools):
    assert tools.resolve("79132") == 79132  # Inception's id; no movie is titled "79132"


def test_absent_number_title_is_not_guessed(tools):
    with pytest.raises(ToolError):
        tools.resolve("The Matrix Reloaded 2")


@pytest.mark.parametrize("query", ["Oldboy", "The Goonies", "Solaris 1972"])
def test_fragment_and_wrong_year_are_not_guessed(tools, query):
    """Regressions: 'Oldboy' -> Boy (2010), 'The Goonies' -> Goon (2011), 'Solaris 1972' -> Solaris (2002)."""
    with pytest.raises(ToolError):
        tools.resolve(query)


@pytest.mark.parametrize("query", ["Big", "Up", "Elf", "M", "Heat", "Seven", "Solaris"])
def test_short_real_titles_still_resolve(tools, data, query):
    assert data.movies.loc[tools.resolve(query), "display"].lower() == query.lower()


def test_every_golden_title_exists(tools):
    """A golden title that doesn't resolve would silently weaken the scenario checks."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from eval.scenarios import DARK_TWIST_GOLD, FAMILY_GOLD, OLD_SCIFI_GOLD

    for title in DARK_TWIST_GOLD + FAMILY_GOLD + OLD_SCIFI_GOLD:
        tools.resolve(title)


def test_unexpected_tool_exception_is_reported_not_raised(tools, monkeypatch):
    def boom(**_):
        raise KeyError("missing")

    monkeypatch.setattr(tools, "get_user_profile", boom)
    text, is_error = tools.call("get_user_profile", {})
    assert is_error and "KeyError" in text
