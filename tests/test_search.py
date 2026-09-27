"""Search and request-driven recommendation paths, offline.

The real content index needs plot embeddings (an API call or a 29-minute local build), so these tests use a small
synthetic index whose vectors are the movies' genres. That makes relevance predictable ("comedy" -> comedies) while
exercising the same code: stage-1 scoring, constraints, attribute filters, re-rank hand-off and the two-stage
recommend path.
"""

import sys
from pathlib import Path

import numpy as np
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.attributes import MOODS, MovieAttributes  # noqa: E402
from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.content import ContentIndex  # noqa: E402
from movie_agent.data import MovieData, genre_matrix  # noqa: E402
from movie_agent.memory import MemoryStore  # noqa: E402
from movie_agent.recommender import Recommender  # noqa: E402
from movie_agent.rerank import NoRerank  # noqa: E402
from movie_agent.tools import MovieTools  # noqa: E402

USER = 15


class GenreEncoder:
    """Maps a query onto the genre axes it names, e.g. 'a funny comedy' -> the Comedy axis."""

    def __init__(self, genres: list[str]):
        self.genres = genres

    def encode_query(self, query: str) -> np.ndarray:
        q = query.lower()
        v = np.array([1.0 if g.lower() in q else 0.0 for g in self.genres], dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)


@pytest.fixture(scope="module")
def data():
    return MovieData.load()


@pytest.fixture(scope="module")
def cf(data):
    return CFModel(data)


@pytest.fixture(scope="module")
def content(data):
    genres = data.all_genres
    vecs = genre_matrix(data.movies, genres).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    docs = [f"{row['display']} {' '.join(row['genres'])}" for _, row in data.movies.iterrows()]
    tfidf = TfidfVectorizer()
    idx = ContentIndex(
        data=data,
        movie_vecs=vecs,
        chunk_vecs=vecs,
        chunk_owner=np.arange(len(vecs)),
        tfidf=tfidf,
        tfidf_mat=tfidf.fit_transform(docs),
        backend="test",
    )
    idx._encoder = GenreEncoder(genres)
    return idx


@pytest.fixture(scope="module")
def attributes(data):
    """Every known movie is 'funny' with violence 3, except comedies, which are violence 0."""
    n = len(data.movies)
    is_comedy = np.array(["Comedy" in gs for gs in data.movies["genres"]])
    moods = np.zeros((n, len(MOODS)), dtype=bool)
    moods[:, MOODS.index("funny")] = True
    return MovieAttributes(
        moods=moods, twist=np.zeros(n), violence=np.where(is_comedy, 0.0, 3.0), known=np.ones(n, dtype=bool)
    )


@pytest.fixture
def tools(data, cf, content, attributes):
    t = MovieTools(
        data=data,
        cf=cf,
        content=content,
        rec=Recommender(data, cf, content),
        memory=MemoryStore(":memory:"),
        reranker=NoRerank(),
        attributes=attributes,
    )
    t.set_user(USER)
    return t


def _genres(tools, movie_id):
    return tools.data.movies.loc[movie_id, "genres"]


def test_search_finds_the_requested_genre_and_hides_rated_movies(tools):
    out = tools.search_movies("a western", n=8)
    ids = [r["movie_id"] for r in out["results"]]
    assert len(ids) == 8
    assert all("Western" in _genres(tools, m) for m in ids)
    rated = set(tools.data.user_ratings[USER].index)
    assert not rated & set(ids)
    assert out["reranker"]["kind"] == "none"
    assert all("matching_plot_excerpt" in r and "for_you" in r for r in out["results"])


def test_search_respects_constraints(tools):
    out = tools.search_movies("a war film", n=5, min_year=1950, max_year=1969, exclude_genres=["Drama"])
    for r in out["results"]:
        year = tools.data.movies.loc[r["movie_id"], "year"]
        assert 1950 <= year <= 1969 and "Drama" not in _genres(tools, r["movie_id"])


def test_search_violence_filter_and_attribute_cards(tools):
    out = tools.search_movies("a thriller", n=5, max_violence=0)
    assert out["results"], "comedies have violence 0 in the fixture, so something must pass"
    for r in out["results"]:
        assert "Comedy" in _genres(tools, r["movie_id"])  # everything else is violence 3 and filtered out
        assert r["attributes"]["violence_0_3"] == 0


def test_search_results_are_not_repeated_by_a_later_recommendation(tools):
    first = {r["movie_id"] for r in tools.search_movies("a musical", n=5)["results"]}
    later = {r["movie_id"] for r in tools.recommend_movies(n=10, include_genres=["Musical"])["recommendations"]}
    assert not first & later


def test_mood_description_uses_the_two_stage_path(tools):
    out = tools.recommend_movies(n=5, mood_or_description="a horror movie")
    recs = out["recommendations"]
    assert len(recs) == 5
    assert all("Horror" in _genres(tools, r["movie_id"]) for r in recs)
    assert out["applied_constraints"]["mood_or_description"] == "a horror movie"


def test_more_like_uses_content_and_never_returns_the_anchor(tools):
    anchor = tools.resolve("Toy Story")
    out = tools.recommend_movies(n=5, more_like=["Toy Story"], exclude_genres=["Animation"])
    ids = [r["movie_id"] for r in out["recommendations"]]
    assert anchor not in ids
    assert not any("Animation" in _genres(tools, m) for m in ids)


def test_relevance_modes_agree_on_an_obvious_query(content, data):
    is_doc = np.array(["Documentary" in gs for gs in data.movies["genres"]])
    for mode in ("dense", "lexical", "hybrid"):
        top = np.argsort(-content.relevance("documentary", mode=mode))[:20]
        assert is_doc[top].mean() > 0.9, mode


def test_already_seen_is_excluded_and_remembered(tools):
    heat, casino = tools.resolve("Heat"), tools.resolve("Casino")
    out = tools.recommend_movies(n=10, include_genres=["Crime"], already_seen=["Heat", "Casino"])
    ids = {r["movie_id"] for r in out["recommendations"]}
    assert not {heat, casino} & ids
    assert set(out["remembered_as_seen"]) == {"Heat (1995)", "Casino (1995)"}
    assert {heat, casino} <= tools.memory.excluded_movie_ids(USER)  # survives into later sessions


def test_already_seen_reports_titles_it_cannot_resolve(tools):
    out = tools.search_movies("a crime film", n=3, already_seen=["The Matrix"])
    assert out["already_seen_not_resolved"][0]["title"] == "The Matrix"
    assert not tools.memory.list(USER)  # nothing wrong was stored


def test_quality_floor_uses_the_raw_mean(tools):
    maid = tools.resolve("Maid to Order")  # 3 ratings, mean 1.83, but Bayesian mean ~3.1
    row = tools.cf.m_index[maid]
    assert not tools.rec.quality_ok(2.75)[row]
    unrated = int(np.argmin(tools.data.movie_stats["count"].to_numpy()))
    assert tools.rec.quality_ok(2.75)[unrated]  # too few ratings to judge: passes
    out = tools.search_movies("a funny comedy", n=15)
    stats = tools.data.movie_stats
    for r in out["results"]:
        st = stats.loc[r["movie_id"]]
        assert st["count"] < 3 or st["mean"] >= 2.75
    assert out["results"] and all(r["movie_id"] != maid for r in out["results"])
