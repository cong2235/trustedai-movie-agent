"""Web UI: chat with the agent, inspect its evidence, give feedback, and monitor the system.

    streamlit run app/streamlit_app.py

Heavy components (data, CF matrices, embeddings, re-ranker) are built once per process and shared;
each browser session gets its own MovieTools/MovieAgent so conversations don't leak into each other.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent import config, monitor, runtime  # noqa: E402
from movie_agent.agent import MovieAgent, detect_provider  # noqa: E402
from movie_agent.telemetry import Telemetry  # noqa: E402
from movie_agent.tools import MovieTools  # noqa: E402

SERIES_1 = "#2a78d6"

st.set_page_config(page_title="Movie Discovery Agent", page_icon="🎬", layout="wide")


def shared_tools() -> MovieTools:
    """Process-wide singleton, pre-warmed at container start by app/serve.py (see movie_agent.runtime)."""
    if not runtime.is_ready():
        with st.spinner("Warming up: loading ratings, CF matrices and plot embeddings (first start only)…"):
            return runtime.shared_tools()
    return runtime.shared_tools()


def new_session_tools() -> MovieTools:
    base = shared_tools()
    return MovieTools(
        data=base.data, cf=base.cf, content=base.content, rec=base.rec, reranker=base.reranker, memory=base.memory
    )


@st.cache_resource
def telemetry() -> Telemetry:
    return Telemetry()


with st.sidebar:
    st.header("Session")
    user_id = st.number_input(
        "User ID (1-610)",
        min_value=1,
        max_value=610,
        value=15,
        step=1,
        help="Suggested: 1 (action/comedy, 190 ratings), 15 (sci-fi, 85), 30 (sparse, 18)",
    )
    provider = detect_provider()
    st.caption(
        f"LLM: **{provider or 'none'}** · embeddings: **{config.EMBED_BACKEND}** · re-ranker: **{config.RERANKER}**"
    )
    if (
        st.button("New conversation", width="stretch")
        or "agent" not in st.session_state
        or st.session_state.get("user_id") != user_id
    ):
        if provider is None:
            st.session_state.agent = None
        else:
            st.session_state.agent = MovieAgent(tools=new_session_tools(), telemetry=telemetry())
            st.session_state.agent.start(int(user_id))
        st.session_state.user_id = user_id
        st.session_state.history = []
    if provider is None:
        st.error("No ANTHROPIC_API_KEY / OPENAI_API_KEY found. Put one in .env (see .env.example).")
    st.divider()
    st.subheader("Long-term memory")
    mems = shared_tools().memory.list(int(user_id))
    if not mems:
        st.caption("Nothing remembered yet. Try: *I've already seen Forrest Gump* or *I'm tired of animated movies*.")
    for m in mems:
        what = m["note"] or shared_tools().data.label(m["movie_id"])
        c1, c2 = st.columns([5, 1])
        c1.caption(f"**{m['kind']}** · {what}")
        if c2.button("✕", key=f"forget{m['memory_id']}", help="Forget this"):
            shared_tools().memory.forget(int(user_id), m["memory_id"])
            st.rerun()
    st.divider()
    st.caption(
        "Try: *What should I watch tonight?* · *What do people with similar taste think about Pulp Fiction?* · "
        "*I liked Toy Story but I'm tired of animated movies* · *What's my blind spot?*"
    )

chat_tab, monitor_tab = st.tabs(["💬 Chat", "📈 Monitor"])

with chat_tab:
    for i, msg in enumerate(st.session_state.get("history", [])):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                meta = msg["meta"]
                badge = "🛠️ revised by grounding check" if meta["revised"] else "✅ grounding check passed"
                ttft = f" · first token {meta['ttft_s']} s" if meta.get("ttft_s") else ""
                st.caption(
                    f"{meta['latency_s']} s{ttft} · {meta['tokens']} tokens · {len(meta['calls'])} tool calls · {badge}"
                )
                with st.expander("Evidence: tool calls and outputs"):
                    for c in meta["calls"]:
                        st.markdown(
                            f"**`{c['tool']}`** `{json.dumps(c['input'], ensure_ascii=False)}` · {c['ms']} ms"
                            + (" · ❌ error" if c["is_error"] else "")
                        )
                        st.json(c["output"], expanded=False)
                cols = st.columns([1, 1, 12])
                if cols[0].button("👍", key=f"up{i}"):
                    telemetry().log_feedback(meta["turn_id"], 1)
                    st.toast("Thanks - logged")
                if cols[1].button("👎", key=f"down{i}"):
                    telemetry().log_feedback(meta["turn_id"], -1)
                    st.toast("Thanks - logged")

    prompt = st.chat_input("Ask about movies…", disabled=st.session_state.get("agent") is None)
    if prompt:
        st.session_state.history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            status = st.status("Investigating the data…", expanded=False)
            box = st.empty()
            streamed: list[str] = []

            def on_token(delta: str) -> None:
                if not streamed:
                    status.update(label="Writing the answer…")
                streamed.append(delta)
                box.markdown("".join(streamed) + "▌")

            res = st.session_state.agent.ask(prompt, on_token=on_token)
            status.update(label=f"Done in {res.latency_s} s", state="complete")
        st.session_state.history.append(
            {
                "role": "assistant",
                "content": res.text,
                "meta": {
                    "turn_id": res.turn_id,
                    "latency_s": res.latency_s,
                    "revised": res.revised,
                    "ttft_s": res.ttft_s,
                    "tokens": res.usage["input_tokens"] + res.usage["output_tokens"],
                    "calls": res.tool_calls,
                },
            }
        )
        st.rerun()

with monitor_tab:
    window = st.radio("Window", ["1 h", "24 h", "7 d", "All"], index=3, horizontal=True)
    since = {"1 h": 3600, "24 h": 86400, "7 d": 7 * 86400}.get(window)
    turns, calls = monitor.load(since_ts=time.time() - since if since else None)
    k = monitor.kpis(turns, calls)
    if not k.get("turns"):
        st.info("No traffic logged yet. Chat, or run `python scripts/run_scenarios.py --mode llm` to generate some.")
    else:
        breaches = monitor.alerts(k)
        if breaches:
            for b in breaches:
                st.warning("⚠️ SLO breach: " + b)
        else:
            st.success("✅ All SLOs met")

        def tile(col, label, key, fmt="{}", slo_key=None):
            v = k.get(key)
            slo = monitor.SLOS.get(slo_key or key)
            ok = None if (slo is None or v is None) else (v <= slo[1] if slo[0] == "<=" else v >= slo[1])
            icon = "" if ok is None else ("✅ " if ok else "⚠️ ")
            col.metric(
                label, "n/a" if v is None else fmt.format(v), help=None if slo is None else f"SLO {slo[0]} {slo[1]}"
            )
            if icon:
                col.caption(icon + ("within SLO" if ok else "breaches SLO"))

        r1 = st.columns(5)
        tile(r1[0], "Turns", "turns")
        tile(r1[1], "Latency p50", "latency_p50_s", "{:.1f} s")
        tile(r1[2], "Latency p95", "latency_p95_s", "{:.1f} s")
        r1[2].caption(f"first token p50: {k['ttft_p50_s']} s" if k.get("ttft_p50_s") else "")
        tile(r1[3], "Tool error rate", "tool_error_rate", "{:.1%}")
        tile(r1[4], "Cost / turn", "cost_per_turn_usd", "${:.4f}")
        r2 = st.columns(5)
        tile(r2[0], "Guardrail triggers", "guardrail_trigger_rate", "{:.1%}")
        tile(r2[1], "Fixed by revision", "revision_fix_rate", "{:.0%}")
        tile(r2[2], "Re-rank p95", "rerank_p95_ms", "{} ms")
        tile(r2[3], "Re-rank errors", "rerank_error_rate", "{:.1%}")
        tile(r2[4], "Thumbs-up rate", "thumbs_up_rate", "{:.0%}")

        ts = monitor.timeseries(turns, "1h" if window in ("1 h", "24 h") else "1D")
        st.subheader("Over time")
        c1, c2 = st.columns(2)
        c3, c4 = st.columns(2)
        for col, name, title in (
            (c1, "turns", "Turns"),
            (c2, "latency_p95_s", "Latency p95 (s)"),
            (c3, "cost_usd", "Cost (USD)"),
            (c4, "guardrail_rate", "Guardrail trigger rate"),
        ):
            col.caption(title)
            col.line_chart(ts[[name]], color=SERIES_1, height=180)

        st.subheader("Per tool")
        st.dataframe(monitor.per_tool(calls), width="stretch")
        st.subheader("Guardrail triggers")
        ev = monitor.guardrail_events(turns)
        st.dataframe(ev, width="stretch", hide_index=True) if len(ev) else st.caption("none")
        st.subheader("Recent turns")
        recent = turns.sort_values("ts", ascending=False).head(50)[
            [
                "time",
                "user_id",
                "model",
                "question",
                "latency_ms",
                "n_tool_calls",
                "n_tool_errors",
                "guardrail_issues",
                "revised",
                "cost_usd",
                "feedback",
            ]
        ]
        st.dataframe(recent, width="stretch", hide_index=True)
