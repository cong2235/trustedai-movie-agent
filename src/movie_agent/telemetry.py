"""Structured logs and metrics for every conversation turn.

Two sinks, one call site:
  * logs/app.jsonl       one JSON object per event (turn, tool call, re-rank, guardrail) for grep / log shipping
  * logs/telemetry.db    SQLite tables `turns` and `tool_calls` that the monitor dashboard queries

SQLite keeps this dependency-free and good enough for one process; the schema maps 1:1 onto a Postgres table
or an OpenTelemetry span (turn = root span, tool call = child span) if this were deployed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from . import config

# $ per 1M tokens (input, output) for cost tracking; unknown models count as 0 and are flagged in the dashboard
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY, ts REAL, session_id TEXT, user_id INTEGER, provider TEXT, model TEXT,
    question TEXT, answer TEXT, latency_ms INTEGER, llm_calls INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, n_tool_calls INTEGER, n_tool_errors INTEGER, stop_reason TEXT,
    guardrail_issues INTEGER, guardrail_detail TEXT, revised INTEGER, issues_after_revision INTEGER,
    feedback INTEGER, error TEXT, ttft_ms INTEGER, context_messages INTEGER, llm_call_detail TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    turn_id TEXT, ts REAL, tool TEXT, args TEXT, ms INTEGER, is_error INTEGER, output_chars INTEGER,
    rerank_kind TEXT, rerank_ms INTEGER, rerank_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
"""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * p_in + output_tokens * p_out) / 1e6


class Telemetry:
    def __init__(self, db_path=config.TELEMETRY_DB, jsonl_path=config.LOG_DIR / "app.jsonl", enabled: bool = True):
        self.enabled = enabled
        self.db_path, self.jsonl_path = db_path, jsonl_path
        self._lock = threading.Lock()
        if enabled:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with self._conn() as c:
                c.executescript(SCHEMA)
                # additive migration: DBs created by an older version get the new columns
                have = {r[1] for r in c.execute("PRAGMA table_info(turns)")}
                for col, typ in (("ttft_ms", "INTEGER"), ("context_messages", "INTEGER"), ("llm_call_detail", "TEXT")):
                    if col not in have:
                        c.execute(f"ALTER TABLE turns ADD COLUMN {col} {typ}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _jsonl(self, event: dict) -> None:
        with self._lock, open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def log_turn(self, turn: dict, tool_calls: list[dict]) -> None:
        if not self.enabled:
            return
        turn = {**turn, "ts": turn.get("ts", time.time())}
        self._jsonl({"event": "turn", **{k: v for k, v in turn.items() if k != "answer"},
                     "answer_chars": len(turn.get("answer") or "")})
        rows = []
        for t in tool_calls:
            rr = t["output"].get("reranker", {}) if isinstance(t.get("output"), dict) else {}
            rows.append((turn["turn_id"], turn["ts"], t["tool"], json.dumps(t["input"], ensure_ascii=False), t["ms"],
                         int(t["is_error"]), t["output_chars"], rr.get("kind"), rr.get("ms"), rr.get("error")))
            self._jsonl({"event": "tool_call", "turn_id": turn["turn_id"], "tool": t["tool"], "args": t["input"],
                         "ms": t["ms"], "is_error": t["is_error"], "output_chars": t["output_chars"],
                         **({"rerank": rr} if rr else {})})
        cols = ["turn_id", "ts", "session_id", "user_id", "provider", "model", "question", "answer", "latency_ms",
                "llm_calls", "input_tokens", "output_tokens", "cost_usd", "n_tool_calls", "n_tool_errors", "stop_reason",
                "guardrail_issues", "guardrail_detail", "revised", "issues_after_revision", "feedback", "error",
                "ttft_ms", "context_messages", "llm_call_detail"]
        with self._lock, self._conn() as c:
            c.execute(f"INSERT OR REPLACE INTO turns ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                      [turn.get(k) for k in cols])
            c.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def log_feedback(self, turn_id: str, score: int) -> None:
        """score: +1 thumbs up, -1 thumbs down."""
        if not self.enabled:
            return
        self._jsonl({"event": "feedback", "turn_id": turn_id, "score": score, "ts": time.time()})
        with self._lock, self._conn() as c:
            c.execute("UPDATE turns SET feedback = ? WHERE turn_id = ?", (score, turn_id))
