"""Offline tests for the web server (app/server.py): the page it serves and the evidence it builds for the UI."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ["MOVIE_AGENT_TELEMETRY"] = "0"

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_spec = importlib.util.spec_from_file_location("movie_server", ROOT / "app" / "server.py")
server = importlib.util.module_from_spec(_spec)
sys.modules["movie_server"] = server
_spec.loader.exec_module(server)

REC = {
    "tool": "recommend_movies",
    "is_error": False,
    "output": {
        "recommendations": [
            {
                "title": "Braveheart (1995)",
                "genres": ["Action", "Drama", "War"],
                "avg_rating": 4.03,
                "n_ratings": 237,
                "because_you_rated": [
                    {"title": "Forrest Gump (1994)", "your_rating": 5.0, "co_rating_similarity": 0.32}
                ],
                "similar_plots_you_liked": [{"title": "Gladiator (2000)", "your_rating": 5.0, "plot_similarity": 0.42}],
                "similar_users_who_rated_it": {"n": 20, "avg_rating": 4.42, "n_rated_4_or_higher": 19},
                "predicted_rating_for_you": 4.2,
            },
            {"title": "The Silence of the Lambs (1991)", "genres": ["Crime"], "avg_rating": 4.16},
            {"title": "Heat (1995)", "genres": ["Crime"], "avg_rating": 3.95},
        ]
    },
}


def test_cards_follow_the_answer_and_skip_unnamed_movies():
    answer = "1. **The Silence of the Lambs (1991)** ...\n2. **Braveheart (1995)** ..."
    cards = server._cards([REC], answer)
    assert [c["title"] for c in cards] == ["The Silence of the Lambs", "Braveheart"]
    brave = cards[1]
    assert brave["year"] == 1995 and brave["pred"] == 4.2 and brave["n"] == 237
    assert brave["because"] == [{"title": "Forrest Gump", "your_rating": 5.0, "co": 0.32}]
    assert brave["plots"] == [{"title": "Gladiator", "your_rating": 5.0, "plot": 0.42}]
    assert brave["sim"] == {"n": 20, "avg": 4.42, "hi": 19}


def test_cards_use_search_evidence_and_ignore_errors():
    search = {
        "tool": "search_movies",
        "is_error": False,
        "output": {
            "results": [
                {
                    "title": "Fracture (2007)",
                    "avg_rating": 3.67,
                    "for_you": {"because_you_rated": [], "predicted_rating_for_you": 3.9},
                }
            ]
        },
    }
    failed = {"tool": "recommend_movies", "is_error": True, "output": {"error": "x"}}
    cards = server._cards([search, failed], "Try **Fracture (2007)**.")
    assert len(cards) == 1 and cards[0]["pred"] == 3.9 and cards[0]["sim"] is None


def test_opinion_scorecard_from_the_last_opinion_call():
    call = {
        "tool": "similar_users_opinion",
        "is_error": False,
        "output": {
            "movie": "Pulp Fiction (1994)",
            "your_rating": None,
            "predicted_rating_for_you": 4.1,
            "everyone": {"n": 307, "avg_rating": 4.2},
            "similar_users": {
                "n": 20,
                "weighted_avg_rating": 3.89,
                "n_rated_4_or_higher": 13,
                "n_rated_2_5_or_lower": 2,
                "similarity_range": [0.31, 0.38],
            },
            "reliability": "high",
        },
    }
    o = server._opinion([REC, call])
    assert o["movie"] == "Pulp Fiction" and o["year"] == 1994
    assert o["you"] is None and o["pred"] == 4.1 and o["sim"] == 3.89 and o["nAll"] == 307
    assert server._opinion([REC]) is None


def test_index_serves_the_page_in_live_mode():
    client = TestClient(server.app)
    r = client.get("/")
    assert r.status_code == 200
    assert 'window.AGENT_API = "/api"' in r.text
    assert r.text.index("window.AGENT_API") < r.text.index("const API =")
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/favicon.ico").status_code == 200


def test_chat_rejects_bad_input():
    client = TestClient(server.app)
    r = client.post("/api/chat", json={"user_id": 9999, "message": "hi", "session_id": "s"})
    assert r.status_code == 422
