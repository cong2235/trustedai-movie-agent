"""Which connection phase holds the stall, and does connection reuse remove it?

The first probe (probe_llm_latency.py) showed a slow call waiting 16.8 s for response headers while the server
reported 633 ms of processing, so the time is lost before the model. This probe times every httpcore phase
(connect_tcp, start_tls, send, receive_response_headers) of real streamed chat requests.

A/B mode: two connection pools take turns - the OpenAI SDK default (idle keep-alive 5 s) and the app's shared pool
(config.HTTP_KEEPALIVE_S) - with a pause between requests, so each pool sits idle like a user reading an answer.
Same network, same minutes, interleaved.

    python scripts/probe_connection_phases.py --n 60 --pause 10   -> outputs/eval/connection_phases.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config  # noqa: E402


def timed_request(client: httpx.Client, headers: dict, body: dict) -> dict:
    events = []
    t0 = time.perf_counter()

    def trace(name, info):
        events.append((name, round(time.perf_counter() - t0, 3)))

    try:
        with client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            json=body,
            headers=headers,
            extensions={"trace": trace},
        ) as r:
            t_headers = time.perf_counter() - t0
            for _ in r.iter_bytes():  # read to the end so the connection goes back to the pool
                pass
        connect = [t for n, t in events if n == "connection.connect_tcp.complete"]
        started = [t for n, t in events if n == "connection.connect_tcp.started"]
        return {
            "headers_s": round(t_headers, 3),
            "server_ms": int(r.headers.get("openai-processing-ms", -1)),
            "new_connection": bool(started),
            "connect_tcp_s": round(connect[0] - started[0], 3) if connect and started else 0.0,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200], "new_connection": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60, help="total requests, split between the two pools")
    ap.add_argument("--pause", type=float, default=10.0, help="seconds between requests")
    args = ap.parse_args()

    pools = {
        "sdk_default_keepalive_5s": httpx.Client(limits=httpx.Limits(keepalive_expiry=5.0), timeout=40.0),
        f"shared_pool_keepalive_{config.HTTP_KEEPALIVE_S:g}s": httpx.Client(
            limits=httpx.Limits(keepalive_expiry=config.HTTP_KEEPALIVE_S), timeout=40.0
        ),
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_completion_tokens": 5,
        "stream": True,
    }
    rows = []
    names = list(pools)
    for i in range(args.n):
        name = names[i % 2]
        r = {"i": i, "pool": name, **timed_request(pools[name], headers, body)}
        rows.append(r)
        print(
            i,
            name,
            r.get("headers_s"),
            "NEW" if r["new_connection"] else "reused",
            r.get("connect_tcp_s", ""),
            r.get("error", ""),
            flush=True,
        )
        time.sleep(args.pause)

    summary = {}
    for name in names:
        rs = [r for r in rows if r["pool"] == name and "error" not in r]
        h = np.array([r["headers_s"] for r in rs])
        summary[name] = {
            "n": len(rs),
            "errors": sum(1 for r in rows if r["pool"] == name and "error" in r),
            "new_connections": sum(r["new_connection"] for r in rs),
            "stalls_over_5s": int((h > 5).sum()),
            "headers_p50_s": round(float(np.percentile(h, 50)), 3),
            "headers_p95_s": round(float(np.percentile(h, 95)), 3),
            "headers_max_s": round(float(h.max()), 3),
        }
    out = config.OUTPUT_DIR / "eval" / "connection_phases.json"
    out.write_text(json.dumps({"pause_s": args.pause, "summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
