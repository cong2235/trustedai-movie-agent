"""Where do the ~12 s first-chunk stalls come from? Server queueing or the client/network?

For each streamed call, compare the client's time-to-headers and time-to-first-chunk with the server's own
`openai-processing-ms` header. If a stall shows up in the client timings but not in processing-ms, the time is spent
outside the model (connection setup, proxies, retries); if processing-ms carries it, it is server-side.

    python scripts/probe_llm_latency.py --n 30          -> outputs/eval/latency_probe.json
Cost: ~$0.01 per 30 calls with gpt-4o-mini.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config  # noqa: E402
from movie_agent.agent import SYSTEM_PROMPT  # noqa: E402
from movie_agent.tools import TOOL_SCHEMAS  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
    }
    for t in TOOL_SCHEMAS
]


def one_call(client, model: str, with_tools: bool, question: str) -> dict:
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}],
        temperature=0.2,
        max_completion_tokens=300,
        stream=True,
        stream_options={"include_usage": True},
    )
    if with_tools:
        kwargs["tools"] = TOOLS
    t0 = time.perf_counter()
    raw = client.chat.completions.with_raw_response.create(**kwargs)
    t_headers = time.perf_counter() - t0
    stream = raw.parse()
    t_first, cached = None, None
    for chunk in stream:
        if t_first is None and (chunk.choices or chunk.usage):
            t_first = time.perf_counter() - t0
        if chunk.usage is not None and chunk.usage.prompt_tokens_details is not None:
            cached = chunk.usage.prompt_tokens_details.cached_tokens
    return {
        "headers_s": round(t_headers, 3),
        "first_chunk_s": round(t_first or 0.0, 3),
        "total_s": round(time.perf_counter() - t0, 3),
        "server_processing_ms": int(raw.headers.get("openai-processing-ms", -1)),
        "request_id": raw.headers.get("x-request-id"),
        "cached_tokens": cached,
        "with_tools": with_tools,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--retries", type=int, default=0)
    args = ap.parse_args()

    import openai

    client = openai.OpenAI(timeout=args.timeout, max_retries=args.retries)
    questions = ["gợi ý cho tôi vài phim hành động", "What should I watch tonight?", "Something like Alien, no horror."]
    rows = []
    for i in range(args.n):
        try:
            rows.append(one_call(client, args.model, with_tools=i % 2 == 0, question=questions[i % 3]))
        except Exception as e:
            rows.append({"error": f"{type(e).__name__}: {e}"[:200], "with_tools": i % 2 == 0})
        r = rows[-1]
        print(
            i, r.get("headers_s"), r.get("first_chunk_s"), r.get("server_processing_ms"), r.get("error", ""), flush=True
        )

    ok = [r for r in rows if "error" not in r]
    fc = np.array([r["first_chunk_s"] for r in ok])
    slow = [r for r in ok if r["first_chunk_s"] > 5]
    summary = {
        "model": args.model,
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "first_chunk_p50_s": float(np.percentile(fc, 50)),
        "first_chunk_p95_s": float(np.percentile(fc, 95)),
        "share_over_5s": float((fc > 5).mean()),
        "slow_calls": slow,
        "fast_server_ms_p50": float(np.median([r["server_processing_ms"] for r in ok if r["first_chunk_s"] <= 5])),
    }
    out = config.OUTPUT_DIR / "eval" / f"latency_probe_{args.model}.json"
    out.write_text(json.dumps({"summary": summary, "calls": rows}, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "slow_calls"}, indent=1))
    for r in slow:
        print("SLOW", r)


if __name__ == "__main__":
    main()
