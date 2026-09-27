"""One shared HTTP connection pool for every OpenAI call (agent, query embeddings, re-ranker).

Why (measured, scripts/probe_connection_phases.py): ~9% of LLM calls waited 11-17 s before the first byte while the
server reported ~0.5 s of processing. Every slow call spent the time in `connect_tcp`, i.e. opening a new connection,
whose DNS lookup hit an unresponsive resolver before falling back (api.openai.com has a 10-36 s DNS TTL, so the cache
is almost always cold). A new connection was needed on nearly every call because the SDK's default pool drops idle
connections after 5 s, and each client (agent, embedder, re-ranker, every new session) had its own pool.

A single pool with a long keep-alive means a conversation reuses one warm TLS connection: no DNS lookup, no TCP or
TLS handshake. It must speak HTTP/2: over HTTP/1.1 the SDK closes a streamed response right after the [DONE] event,
before the chunked body's end, so the connection could never be reused and every streamed call reconnected anyway.
Over HTTP/2 that only resets one stream. The hedging client in agent.py deliberately keeps its own pool.
"""

from __future__ import annotations

import threading

import httpx

from . import config

_lock = threading.Lock()
_pool: dict[str, httpx.Client] = {}


def _http2_available() -> bool:
    try:
        import h2  # noqa: F401  (httpx[http2])
    except ImportError:
        return False
    return True


def shared_http_client() -> httpx.Client:
    with _lock:
        if "c" not in _pool:
            _pool["c"] = httpx.Client(
                http2=_http2_available(),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=config.HTTP_KEEPALIVE_S,
                ),
                timeout=httpx.Timeout(60.0, connect=10.0),
                follow_redirects=True,
            )
        return _pool["c"]


def openai_client(timeout: float, max_retries: int):
    """An OpenAI client on the shared pool. Per-request timeouts and retries stay per client."""
    import openai

    return openai.OpenAI(timeout=timeout, max_retries=max_retries, http_client=shared_http_client())
