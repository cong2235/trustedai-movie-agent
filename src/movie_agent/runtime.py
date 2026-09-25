"""Process-wide shared components for the web app.

Streamlit re-executes the page script per session, but imported modules persist for the life of the process,
so one lock-protected singleton here is shared by every session. `warm_in_background()` is called by
app/serve.py at process start, so the ~40 s of matrix building happens before the first visitor, not during
their first request (measured: first recommend 17 s cold vs 0.1 s warm).
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_state: dict = {}


def shared_tools():
    from .tools import MovieTools
    with _lock:
        if "tools" not in _state:
            t0 = time.time()
            print("[warm-up] building data, CF matrices, SVD and embeddings index...", flush=True)
            _state["tools"] = MovieTools.build()
            _state["ready_s"] = round(time.time() - t0, 1)
            print(f"[warm-up] ready in {_state['ready_s']} s", flush=True)
    return _state["tools"]


def is_ready() -> bool:
    return "tools" in _state


def warm_in_background() -> threading.Thread:
    th = threading.Thread(target=shared_tools, name="warm-up", daemon=True)
    th.start()
    return th
