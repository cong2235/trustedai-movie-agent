"""Process-wide shared components for the web app.

The web server handles every conversation in one process, so one lock-protected singleton here is shared by
every session. `warm_in_background()` is called by app/server.py at process start, so the ~40 s of matrix
building happens before the first visitor, not during their first request (measured: first recommend 17 s cold
vs 0.1 s warm).
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict = {}


def shared_tools():
    from .tools import MovieTools

    with _lock:
        if "tools" not in _state:
            t0 = time.time()
            log.info("warm-up: building data, CF matrices, SVD and embeddings index")
            _state["tools"] = MovieTools.build()
            _state["ready_s"] = round(time.time() - t0, 1)
            log.info("warm-up: ready in %.1f s", _state["ready_s"])
    return _state["tools"]


def is_ready() -> bool:
    return "tools" in _state


def warm_in_background() -> threading.Thread:
    th = threading.Thread(target=shared_tools, name="warm-up", daemon=True)
    th.start()
    return th
