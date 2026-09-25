"""Interactive CLI.

    python -m movie_agent.cli --user 15            # chat with the LLM agent (Claude or OpenAI key, see .env.example)
    python -m movie_agent.cli --user 15 --no-llm   # drive the tools directly with slash commands

In-chat commands (both modes): /user N, /trace (show last turn's tool calls), /quit
LLM mode also: /good, /bad (feedback on the last answer, logged), /stats (live KPIs from logs/telemetry.db)
No-LLM commands: /profile, /recommend [n], /search <text>, /opinion <title>, /why <title>, /blind, /similar
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import detect_provider
from .render import render
from .tools import MovieTools

console = Console()


def show_trace(calls: list[dict]) -> None:
    for c in calls:
        status = "[red]error[/red]" if c["is_error"] else "ok"
        console.print(f"  [cyan]{c['tool']}[/cyan]({json.dumps(c['input'], ensure_ascii=False)}) -> {status}, "
                      f"{c['output_chars']} chars, {c['ms']} ms")


NO_LLM_COMMANDS = {
    "/profile": lambda t, a: ("get_user_profile", {}),
    "/recommend": lambda t, a: ("recommend_movies", {"n": int(a) if a.strip().isdigit() else 5}),
    "/search": lambda t, a: ("search_movies", {"query": a, "n": 5}),
    "/opinion": lambda t, a: ("similar_users_opinion", {"movie": a}),
    "/why": lambda t, a: ("explain_match", {"movie": a}),
    "/blind": lambda t, a: ("genre_blind_spots", {}),
    "/similar": lambda t, a: ("find_similar_users", {"k": 5}),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, required=True)
    ap.add_argument("--no-llm", action="store_true", help="use tools directly (no API key needed)")
    args = ap.parse_args()

    console.print("[dim]loading data, CF model and embeddings...[/dim]")
    tools = MovieTools.build()
    llm = not args.no_llm
    if llm and detect_provider() is None:
        console.print("[yellow]No ANTHROPIC_API_KEY / OPENAI_API_KEY found - falling back to --no-llm mode.[/yellow]")
        llm = False
    agent = None
    if llm:
        from .agent import MovieAgent
        agent = MovieAgent(tools=tools)
        info = agent.start(args.user)
    else:
        info = tools.set_user(args.user)
    console.print(Panel(f"User {info['user_id']} · {info['n_ratings']} ratings ({info['history_size']} history) · "
                        f"mode: {agent.provider + ' agent (' + agent.model + ')' if llm else 'tools only'}\n"
                        + ("Ask anything, e.g. 'What should I watch tonight?'" if llm else
                           "Commands: " + " ".join(NO_LLM_COMMANDS)), title="Movie discovery assistant"))
    last_calls: list[dict] = []
    last_turn_id = None
    while True:
        try:
            text = console.input("[bold green]you>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/trace":
            show_trace(last_calls)
            continue
        if text in ("/good", "/bad") and agent and last_turn_id:
            agent.telemetry.log_feedback(last_turn_id, 1 if text == "/good" else -1)
            console.print("[dim]feedback logged[/dim]")
            continue
        if text == "/stats":
            from . import monitor
            k = monitor.kpis(*monitor.load())
            console.print(k)
            for a in monitor.alerts(k):
                console.print(f"[yellow]!! {a}[/yellow]")
            continue
        if text.startswith("/user"):
            uid = int(text.split()[1])
            info = agent.start(uid) if agent else tools.set_user(uid)
            console.print(f"switched to user {uid} ({info['n_ratings']} ratings)")
            continue
        if agent:
            with console.status("investigating..."):
                res = agent.ask(text)
            last_calls, last_turn_id = res.tool_calls, res.turn_id
            show_trace(last_calls)
            console.print(Markdown(res.text))
            badge = "revised by grounding check" if res.revised else "grounding check passed"
            console.print(f"[dim]{res.latency_s}s · {res.usage['input_tokens']} in / {res.usage['output_tokens']} out "
                          f"tokens · {badge} · /good /bad to rate[/dim]")
        else:
            cmd, _, rest = text.partition(" ")
            if cmd not in NO_LLM_COMMANDS:
                console.print("Unknown command. " + " ".join(NO_LLM_COMMANDS))
                continue
            name, targs = NO_LLM_COMMANDS[cmd](tools, rest)
            start = len(tools.trace)
            out, _ = tools.call(name, targs)
            last_calls = tools.trace[start:]
            console.print(Markdown(render(name, json.loads(out))))


if __name__ == "__main__":
    sys.exit(main())
