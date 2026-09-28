"""Web server: serves app/chat.html and the JSON API it talks to.

    python app/server.py            # http://localhost:8501

Heavy components (data, CF matrices, embeddings, re-ranker) are built once per process, warmed in the
background at start-up; each browser conversation gets its own MovieTools/MovieAgent so sessions don't leak.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from movie_agent import config, monitor, runtime  # noqa: E402
from movie_agent.agent import BACKENDS, MovieAgent, detect_provider  # noqa: E402
from movie_agent.telemetry import Telemetry, cost_usd  # noqa: E402
from movie_agent.tools import MovieTools, ToolError  # noqa: E402

MAX_SESSIONS = 200
YEAR = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")

app = FastAPI(title="Movie Discovery Agent", docs_url="/api/docs", openapi_url="/api/openapi.json")
_telemetry = Telemetry(enabled=os.environ.get("MOVIE_AGENT_TELEMETRY", "1") == "1")
_sessions: OrderedDict[str, dict] = OrderedDict()
_sessions_lock = threading.Lock()


class ChatIn(BaseModel):
    user_id: int = Field(ge=1, le=610)
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=100)


class FeedbackIn(BaseModel):
    turn_id: str
    score: int = Field(ge=-1, le=1)


class SeenIn(BaseModel):
    user_id: int = Field(ge=1, le=610)
    title: str


def _tools() -> MovieTools:
    return runtime.shared_tools()


def _session(session_id: str, user_id: int) -> dict:
    with _sessions_lock:
        s = _sessions.get(session_id)
        if s is None or s["user_id"] != user_id:
            base = _tools()
            tools = MovieTools(
                data=base.data,
                cf=base.cf,
                content=base.content,
                rec=base.rec,
                reranker=base.reranker,
                memory=base.memory,
            )
            agent = MovieAgent(tools=tools, telemetry=_telemetry)
            agent.start(user_id)
            s = {"user_id": user_id, "agent": agent, "lock": threading.Lock()}
            _sessions[session_id] = s
        _sessions.move_to_end(session_id)
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
        return s


def _memories(user_id: int) -> list[dict]:
    t = _tools()
    return [
        {"id": m["memory_id"], "kind": m["kind"], "what": m["note"] or t.data.label(m["movie_id"])}
        for m in t.memory.list(user_id)
    ]


def _output(call: dict):
    out = call.get("output")
    if isinstance(out, str):
        try:
            return json.loads(out)
        except ValueError:
            return out
    return out


def _split_title(label: str) -> tuple[str, int | None]:
    m = YEAR.match(label or "")
    return (m.group(1), int(m.group(2))) if m else (label, None)


def _pairs(rows, key: str, short: str) -> list[dict]:
    return [
        {"title": _split_title(r["title"])[0], "your_rating": r.get("your_rating"), short: r.get(key)}
        for r in rows or []
    ]


def _card(item: dict) -> dict:
    ev = item.get("for_you") or item
    title, year = _split_title(item["title"])
    sim = ev.get("similar_users_who_rated_it")
    return {
        "title": title,
        "year": year,
        "genres": item.get("genres") or [],
        "avg": item.get("avg_rating"),
        "n": item.get("n_ratings"),
        "pred": ev.get("predicted_rating_for_you"),
        "because": _pairs(ev.get("because_you_rated"), "co_rating_similarity", "co"),
        "plots": _pairs(ev.get("similar_plots_you_liked"), "plot_similarity", "plot"),
        "sim": {"n": sim.get("n"), "avg": sim.get("avg_rating"), "hi": sim.get("n_rated_4_or_higher")} if sim else None,
    }


def _cards(calls: list[dict], answer: str) -> list[dict]:
    """Evidence cards for the movies the answer actually names, in the order it names them."""
    items = {}
    for c in calls:
        out = _output(c)
        if c["is_error"] or not isinstance(out, dict):
            continue
        for item in out.get("recommendations") or out.get("results") or []:
            if isinstance(item, dict) and item.get("title"):
                items[item["title"]] = item
    named = []
    for label, item in items.items():
        title, _ = _split_title(label)
        pos = answer.find(title)
        if pos >= 0:
            named.append((pos, item))
    return [_card(item) for _, item in sorted(named, key=lambda x: x[0])]


def _opinion(calls: list[dict]) -> dict | None:
    for c in reversed(calls):
        out = _output(c)
        if c["tool"] != "similar_users_opinion" or c["is_error"] or not isinstance(out, dict):
            continue
        sim = out.get("similar_users") or {}
        if not sim.get("n"):
            return None
        title, year = _split_title(out.get("movie", ""))
        everyone = out.get("everyone") or {}
        return {
            "movie": title,
            "year": year,
            "you": out.get("your_rating"),
            "pred": out.get("predicted_rating_for_you"),
            "sim": sim.get("weighted_avg_rating"),
            "all": everyone.get("avg_rating"),
            "nAll": everyone.get("n"),
            "nSim": sim.get("n"),
            "hi": sim.get("n_rated_4_or_higher", 0),
            "lo": sim.get("n_rated_2_5_or_lower", 0),
            "range": sim.get("similarity_range") or [None, None],
            "reliability": out.get("reliability", ""),
            "note": out.get("note"),
        }
    return None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html = (ROOT / "app" / "chat.html").read_text(encoding="utf-8")
    return html.replace("<script>", '<script>window.AGENT_API = "/api";</script>\n<script>', 1)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="4" fill="#0a0909"/>'
        '<path d="M8 6v12l10-6z" fill="#e0a83a"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ready": runtime.is_ready()}


@app.get("/api/info")
def info() -> dict:
    provider = detect_provider()
    model = os.environ.get("MOVIE_AGENT_MODEL") or (BACKENDS[provider].default_model if provider else None)
    return {
        "provider": provider,
        "model": model,
        "embeddings": config.EMBED_BACKEND,
        "reranker": config.RERANKER,
        "ready": runtime.is_ready(),
    }


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    if detect_provider() is None:
        raise HTTPException(503, "No OPENAI_API_KEY / ANTHROPIC_API_KEY found. Put one in .env (see .env.example).")
    s = _session(body.session_id, body.user_id)
    with s["lock"]:
        before = {m["id"] for m in _memories(body.user_id)}
        agent: MovieAgent = s["agent"]
        res = agent.ask(body.message)
    memory = _memories(body.user_id)
    calls = [
        {"tool": c["tool"], "input": c["input"], "ms": c["ms"], "output": _output(c), "is_error": bool(c["is_error"])}
        for c in res.tool_calls
    ]
    return {
        "turn_id": res.turn_id,
        "text": res.text,
        "calls": calls,
        "latency": res.latency_s,
        "ttft": res.ttft_s,
        "tokens": res.usage["input_tokens"] + res.usage["output_tokens"],
        "cost": cost_usd(agent.model, res.usage["input_tokens"], res.usage["output_tokens"]),
        "revised": res.revised,
        "memory": [m for m in memory if m["id"] not in before],
        "memory_all": memory,
        "cards": _cards(res.tool_calls, res.text),
        "opinion": _opinion(res.tool_calls),
    }


@app.get("/api/memory/{user_id}")
def list_memory(user_id: int) -> list[dict]:
    return _memories(user_id)


@app.post("/api/memory/seen")
def mark_seen(body: SeenIn) -> list[dict]:
    t = _tools()
    try:
        mid = t.resolve(body.title)
    except ToolError as e:
        raise HTTPException(404, str(e)) from e
    t.memory.add(body.user_id, "seen", movie_id=mid)
    return _memories(body.user_id)


@app.delete("/api/memory/{user_id}/{memory_id}")
def forget(user_id: int, memory_id: int) -> list[dict]:
    _tools().memory.forget(user_id, memory_id)
    return _memories(user_id)


@app.post("/api/feedback")
def feedback(body: FeedbackIn) -> dict:
    _telemetry.log_feedback(body.turn_id, body.score)
    return {"ok": True}


@app.get("/api/monitor")
def monitor_view(window_s: float | None = None) -> dict:
    turns, calls = monitor.load(since_ts=time.time() - window_s if window_s else None)
    k = monitor.kpis(turns, calls)
    tools = monitor.per_tool(calls)
    recent = []
    if not turns.empty:
        for _, r in turns.sort_values("ts", ascending=False).head(30).iterrows():
            recent.append(
                {
                    "ts": float(r["ts"]),
                    "user": int(r["user_id"]),
                    "question": r["question"],
                    "latency": round(r["latency_ms"] / 1000, 1),
                    "tools": int(r["n_tool_calls"]),
                    "revised": bool(r["revised"]),
                    "cost": float(r["cost_usd"]),
                    "feedback": None if r["feedback"] != r["feedback"] else int(r["feedback"]),
                }
            )
    return {
        "kpis": k,
        "alerts": monitor.alerts(k),
        "slos": {name: list(v) for name, v in monitor.SLOS.items()},
        "per_tool": [
            {
                "tool": name,
                "calls": int(r["calls"]),
                "error_rate": float(r["error_rate"]),
                "p50_ms": float(r["p50_ms"]),
                "p95_ms": float(r["p95_ms"]),
            }
            for name, r in tools.iterrows()
        ]
        if len(tools)
        else [],
        "recent": recent,
    }


if __name__ == "__main__":
    import uvicorn

    config.setup_logging()
    runtime.warm_in_background()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8501
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
