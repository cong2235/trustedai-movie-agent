"""Long-term, per-user memory that survives across sessions.

The rating history is static data. What a user *tells* the assistant is not: "I've seen Forrest Gump",
"not interested in that one", "I'm tired of animated movies". Without memory, every new session forgets it.
This was the proposed fix for Failure 3 (sparse users get blockbusters they have already seen).

    kinds   preference  free-text standing preference ("prefers films after 1990")
            seen        watched but not rated in the dataset  -> excluded from recommendations
            dismissed   "not interested"                       -> excluded
            disliked    watched and disliked                   -> excluded
            liked       watched and liked                      -> excluded (already watched), and kept as a positive
                                                                   signal the model can use ("more like the one I loved")
            avoid_genre genre the user doesn't want (note=genre)  -> excluded genre on every later request
                        (structured on purpose: the 3x LLM run showed the model *stored* "doesn't like war
                        movies" as free text but did not apply it in the next session, 0/3)

Exclusions are enforced by the tools, not left to the model to remember. Short-term memory (the current
conversation) lives in MovieAgent and is compacted there.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from . import config

KINDS = ("preference", "seen", "dismissed", "disliked", "liked", "avoid_genre")
EXCLUDING = ("seen", "dismissed", "disliked", "liked")   # "liked" also means watched: don't re-suggest it

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
    movie_id INTEGER, note TEXT, created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id);
"""
# SQLite treats NULLs as distinct in a plain unique index, so (user, 'seen', 356, NULL) could be stored twice -
# every movie memory has a NULL note. An expression index over COALESCEd columns makes duplicates impossible.
DEDUPE_AND_INDEX = """
DELETE FROM memories WHERE memory_id NOT IN (
    SELECT MIN(memory_id) FROM memories GROUP BY user_id, kind, COALESCE(movie_id, -1), COALESCE(note, ''));
DROP INDEX IF EXISTS idx_mem_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_unique_v2
    ON memories(user_id, kind, COALESCE(movie_id, -1), COALESCE(note, ''));
"""


class MemoryStore:
    def __init__(self, path: Path | str | None = None):
        """path=None -> config.MEMORY_DB; ':memory:' -> private in-process store (tests)."""
        path = path or config.MEMORY_DB
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.executescript(DEDUPE_AND_INDEX)      # also migrates stores created by the first version
            self._conn.commit()

    def add(self, user_id: int, kind: str, movie_id: int | None = None, note: str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if movie_id is None and not note:
            raise ValueError("a memory needs a movie or a note")
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO memories (user_id, kind, movie_id, note, created_ts) VALUES (?,?,?,?,?)",
                (user_id, kind, movie_id, note, time.time()))
            self._conn.commit()
            if cur.lastrowid and cur.rowcount:
                return cur.lastrowid
            row = self._conn.execute("SELECT memory_id FROM memories WHERE user_id=? AND kind=? AND movie_id IS ? "
                                     "AND note IS ?", (user_id, kind, movie_id, note)).fetchone()
            return row[0]

    def forget(self, user_id: int, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE user_id=? AND memory_id=?", (user_id, memory_id))
            self._conn.commit()
            return cur.rowcount > 0

    def list(self, user_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT memory_id, kind, movie_id, note, created_ts FROM memories "
                                      "WHERE user_id=? ORDER BY created_ts", (user_id,)).fetchall()
        return [{"memory_id": r[0], "kind": r[1], "movie_id": r[2], "note": r[3], "created_ts": r[4]} for r in rows]

    def excluded_movie_ids(self, user_id: int) -> set[int]:
        return {m["movie_id"] for m in self.list(user_id) if m["kind"] in EXCLUDING and m["movie_id"] is not None}

    def avoided_genres(self, user_id: int) -> list[str]:
        return sorted({m["note"] for m in self.list(user_id) if m["kind"] == "avoid_genre" and m["note"]})

    def summary(self, user_id: int, label) -> str:
        """Compact text for the model's context. `label` maps movie_id -> 'Title (Year)'."""
        mems = self.list(user_id)
        if not mems:
            return ""
        parts = []
        prefs = [m["note"] for m in mems if m["kind"] == "preference"]
        if prefs:
            parts.append("stated preferences: " + "; ".join(prefs))
        avoid = self.avoided_genres(user_id)
        if avoid:
            parts.append("avoids genres (auto-excluded by the tools): " + ", ".join(avoid))
        for kind in ("seen", "liked", "disliked", "dismissed"):
            titles = [label(m["movie_id"]) for m in mems if m["kind"] == kind and m["movie_id"] is not None]
            if titles:
                parts.append(f"{kind}: " + ", ".join(titles[-15:]))
        return "Remembered from earlier sessions - " + " | ".join(parts)


class NullMemory(MemoryStore):
    """Memory disabled: same interface, stores nothing."""

    def __init__(self):
        pass

    def add(self, *a, **k):
        return 0

    def forget(self, *a, **k):
        return False

    def list(self, user_id):
        return []


def default_store() -> MemoryStore:
    import os
    return NullMemory() if os.environ.get("MOVIE_AGENT_MEMORY", "1") == "0" else MemoryStore()
