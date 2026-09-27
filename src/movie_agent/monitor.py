"""Operational metrics computed from the telemetry DB, shared by the CLI (scripts/monitor.py) and the dashboard.

What is monitored, and why
  latency p50/p95            the user experience; agent turns are multi-call so the tail matters
  tool error rate            broken tools or bad arguments from the model
  guardrail trigger rate     how often the model's answer drifted from its evidence (hallucination proxy)
  revision fix rate          whether the automatic revision actually repaired it
  cost & tokens per turn     spend, and a leading indicator of context bloat in long sessions
  re-ranker errors/latency   the LLM re-ranker is an external dependency with its own failure mode
  thumbs-up rate             the only direct user signal
Each has an SLO threshold; breaches are returned as alerts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from . import config

SLOS = {
    "latency_p95_s": ("<=", 20.0),
    "tool_error_rate": ("<=", 0.10),
    "guardrail_trigger_rate": ("<=", 0.10),
    "unfixed_guardrail_rate": ("<=", 0.02),
    "rerank_error_rate": ("<=", 0.05),
    "cost_per_turn_usd": ("<=", 0.05),
    "thumbs_up_rate": (">=", 0.70),
}


def load(db_path: Path = config.TELEMETRY_DB, since_ts: float | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not Path(db_path).exists():
        return pd.DataFrame(), pd.DataFrame()
    with sqlite3.connect(db_path) as c:
        where = f" WHERE ts >= {float(since_ts)}" if since_ts else ""
        turns = pd.read_sql(f"SELECT * FROM turns{where}", c)
        calls = pd.read_sql(f"SELECT * FROM tool_calls{where}", c)
    for df in (turns, calls):
        if not df.empty:
            df["time"] = pd.to_datetime(df["ts"], unit="s")
    return turns, calls


def kpis(turns: pd.DataFrame, calls: pd.DataFrame) -> dict:
    if turns.empty:
        return {"turns": 0}
    lat = turns["latency_ms"] / 1000
    rr_all = calls[calls["rerank_kind"].notna() & (calls["rerank_kind"] != "none")] if not calls.empty else calls
    rr = rr_all[rr_all["rerank_kind"] != "llm-cache"] if len(rr_all) else rr_all  # live calls only
    fb = turns["feedback"].dropna()
    k = {
        "turns": int(len(turns)),
        "sessions": int(turns["session_id"].nunique()),
        "users": int(turns["user_id"].nunique()),
        "latency_p50_s": round(float(lat.quantile(0.5)), 2),
        "latency_p95_s": round(float(lat.quantile(0.95)), 2),
        "ttft_p50_s": (
            round(float(turns["ttft_ms"].dropna().quantile(0.5)) / 1000, 2)
            if "ttft_ms" in turns and turns["ttft_ms"].notna().any()
            else None
        ),
        "llm_calls_per_turn": round(float(turns["llm_calls"].mean()), 2),
        **_llm_call_stats(turns),
        "tool_calls_per_turn": round(float(turns["n_tool_calls"].mean()), 2),
        "tool_error_rate": round(float(calls["is_error"].mean()), 3) if not calls.empty else 0.0,
        "guardrail_trigger_rate": round(float(turns["guardrail_issues"].mean()), 3),
        "revision_fix_rate": (
            round(float(1 - turns.loc[turns["revised"] == 1, "issues_after_revision"].mean()), 3)
            if (turns["revised"] == 1).any()
            else None
        ),
        "unfixed_guardrail_rate": round(float((turns["issues_after_revision"] == 1).mean()), 3),
        "input_tokens_per_turn": int(turns["input_tokens"].mean()),
        "output_tokens_per_turn": int(turns["output_tokens"].mean()),
        "cost_per_turn_usd": round(float(turns["cost_usd"].mean()), 5),
        "total_cost_usd": round(float(turns["cost_usd"].sum()), 4),
        "rerank_calls": int(len(rr_all)),
        "rerank_cache_hit_rate": round(float((rr_all["rerank_kind"] == "llm-cache").mean()), 3)
        if len(rr_all)
        else None,
        "rerank_p95_ms": int(rr["rerank_ms"].quantile(0.95)) if len(rr) else None,
        "rerank_error_rate": round(float(rr["rerank_error"].notna().mean()), 3) if len(rr) else 0.0,
        "errors": int(turns["error"].notna().sum()),
        "feedback_count": int(len(fb)),
        "thumbs_up_rate": round(float((fb > 0).mean()), 3) if len(fb) else None,
    }
    return k


def _llm_call_stats(turns: pd.DataFrame) -> dict:
    """Per-LLM-call latency (the dominant cost) and how often tail-latency hedging kicked in."""
    if "llm_call_detail" not in turns:
        return {}
    calls = [c for d in turns["llm_call_detail"].dropna() for c in json.loads(d or "[]")]
    if not calls:
        return {}
    tot = pd.Series([c["total_s"] for c in calls])
    first = pd.Series([c["first_chunk_s"] for c in calls])
    return {
        "llm_call_p50_s": round(float(tot.quantile(0.5)), 2),
        "llm_call_p95_s": round(float(tot.quantile(0.95)), 2),
        "llm_first_chunk_p95_s": round(float(first.quantile(0.95)), 2),
        "hedge_rate": round(sum(c["hedged"] for c in calls) / len(calls), 3),
    }


def alerts(k: dict) -> list[str]:
    out = []
    for name, (op, limit) in SLOS.items():
        v = k.get(name)
        if v is None:
            continue
        ok = v <= limit if op == "<=" else v >= limit
        if not ok:
            out.append(f"{name} = {v} breaches SLO {op} {limit}")
    return out


def per_tool(calls: pd.DataFrame) -> pd.DataFrame:
    if calls.empty:
        return calls
    g = calls.groupby("tool")
    return pd.DataFrame(
        {
            "calls": g.size(),
            "error_rate": g["is_error"].mean().round(3),
            "p50_ms": g["ms"].quantile(0.5).round(0),
            "p95_ms": g["ms"].quantile(0.95).round(0),
            "avg_output_chars": g["output_chars"].mean().round(0),
        }
    ).sort_values("calls", ascending=False)


def timeseries(turns: pd.DataFrame, freq: str = "1h") -> pd.DataFrame:
    if turns.empty:
        return turns
    t = turns.set_index("time").sort_index()
    return pd.DataFrame(
        {
            "turns": t["turn_id"].resample(freq).count(),
            "latency_p95_s": (t["latency_ms"] / 1000).resample(freq).quantile(0.95),
            "cost_usd": t["cost_usd"].resample(freq).sum(),
            "guardrail_rate": t["guardrail_issues"].resample(freq).mean(),
        }
    ).dropna(how="all")


def guardrail_events(turns: pd.DataFrame, limit: int = 20) -> pd.DataFrame:
    g = turns[turns["guardrail_issues"] == 1].sort_values("ts", ascending=False).head(limit)
    rows = []
    for _, r in g.iterrows():
        d = json.loads(r["guardrail_detail"] or "{}")
        first = d.get("first") or {}
        rows.append(
            {
                "time": r["time"],
                "question": r["question"][:80],
                "hallucinated": ", ".join(first.get("hallucinated_titles", [])),
                "ungrounded_titles": ", ".join(first.get("ungrounded_titles", [])),
                "ungrounded_numbers": ", ".join(first.get("ungrounded_numbers", [])),
                "fixed_by_revision": None
                if r["issues_after_revision"] is None
                else not bool(r["issues_after_revision"]),
            }
        )
    return pd.DataFrame(rows)
