"""Run the conversation suite (eval/scenarios.py) and score every turn automatically.

    python scripts/run_scenarios.py --mode scripted              # no API key: reference tool plans + templates
    python scripts/run_scenarios.py --mode llm --judge           # real agent (+ LLM judge)
    python scripts/run_scenarios.py --mode llm --repeat 3        # each scenario 3x: pass *rates*, not anecdotes
    python scripts/run_scenarios.py --mode llm --only u1_terminator_2 --repeat 5
    python scripts/run_scenarios.py --mode llm --suite memory --repeat 3   # hard short/long-term memory suite
    python scripts/run_scenarios.py --mode llm --suite heldout             # held-out set: run once, report as is

Checks per turn (a turn passes only if all apply and hold):
  tools        required tools were called (alternatives separated by '|')
  args         the model passed the right *values*: a movie argument resolves to the intended film,
               list arguments contain the expected genre/title, numeric bounds are respected
  grounding    (shared with the online guardrail) hallucinated titles, titles no tool returned, numbers no tool
               returned, numbers attached to the wrong movie, wrong claims about the user's own ratings
  constraints  recommended movies are unseen / in-genre / in-era / not repeated / not remembered as seen
  golden       at least one recommended movie from a hand-made list of good answers (where one exists)
  text         required statements ("not in the dataset", "you rated it 3")
  memory       long-term memory *after this turn*: memory_has / memory_lacks; forbid_tools (e.g. no remember for a
               one-off request); max_input_tokens (context budget, catches broken history compaction)
  references   {"resolves_to_prev": [turn, k]}: an argument must name the k-th movie recommended in an earlier
               answer (in the order the answer listed them) - "why the second one?" after the history was compacted
Each scenario runs with a private in-memory long-term memory, so runs are independent and never touch
real user data. Outputs: outputs/transcripts_<tag>/*.md and outputs/eval/scenarios_<tag>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from eval.scenarios import SCENARIOS  # noqa: E402
from movie_agent import config  # noqa: E402
from movie_agent.guardrails import ISSUE_KEYS, TitleIndex, check_answer  # noqa: E402
from movie_agent.memory import MemoryStore  # noqa: E402
from movie_agent.render import render  # noqa: E402
from movie_agent.tools import MovieTools, ToolError  # noqa: E402

ABSENT_PHRASES = ["not in the dataset", "isn't in the dataset", "is not in", "isn't in", "not available",
                  "absent", "not present", "doesn't exist in", "does not exist in", "not part of", "no record",
                  "missing from", "couldn't find", "could not find"]
RUN_INFO: dict = {}


# ------------------------------------------------------------------ helpers
def recommended_in(trace: list[dict]) -> list[int]:
    ids = []
    for t in trace:
        out = t["output"]
        for key in ("recommendations", "results"):
            ids += [r["movie_id"] for r in out.get(key, [])]
    return ids


def _resolve(tools: MovieTools, value) -> int | None:
    try:
        return tools.resolve(value)
    except (ToolError, ValueError, TypeError):
        return None


def _arg_ok(tools: MovieTools, value, spec, history=None) -> bool:
    """spec: True (present) | {"resolves_to": title} | {"resolves_to_prev": [turn, k]} | {"contains": x} |
    {"contains_movie": title} | {"max"|"min": n}"""
    if spec is True:
        return value not in (None, "", [], {})
    if "absent_or_below" in spec:                        # a constraint the user has since lifted
        return value in (None, "", []) or float(value) < spec["absent_or_below"]
    if value in (None, "", []):
        return False
    if "resolves_to_prev" in spec:
        t, k = spec["resolves_to_prev"]
        listed = (history or [])[t]["recs_order"] if history and t < len(history) else []
        return k < len(listed) and _resolve(tools, value) == listed[k]
    if "resolves_to" in spec:
        return _resolve(tools, value) == _resolve(tools, spec["resolves_to"])
    if "contains" in spec:
        vals = value if isinstance(value, list) else [value]
        return any(str(v).lower() == str(spec["contains"]).lower() for v in vals)
    if "contains_movie" in spec:
        want = _resolve(tools, spec["contains_movie"])
        return any(_resolve(tools, v) == want for v in (value if isinstance(value, list) else [value]))
    if "max" in spec:
        return float(value) <= spec["max"]
    if "min" in spec:
        return float(value) >= spec["min"]
    raise ValueError(f"unknown arg spec {spec}")


def args_check(turn, trace, tools, history=None) -> list[str]:
    """Every expected (tool, args) group must be satisfied by at least one call of one of the listed tools."""
    problems = []
    for tool_alts, specs in turn.get("expect_args", {}).items():
        alts = tool_alts.split("|")
        calls = [t for t in trace if t["tool"] in alts]
        if not any(all(_arg_ok(tools, c["input"].get(a), sp, history) for a, sp in specs.items()) for c in calls):
            got = [c["input"] for c in calls] or "no call"
            problems.append(f"{tool_alts} expected {specs}, got {got}")
    return problems


def ordered_recs(answer: str, trace: list[dict], tools: MovieTools, scripted: bool) -> list[int]:
    """Recommended movies in the order the *answer* presents them (what "the second one" refers to)."""
    tool_recs = recommended_in(trace)
    if scripted:
        return tool_recs
    found, _ = TitleIndex.for_data(tools.data).mentions(answer)
    return list(dict.fromkeys(m for _, m in found if m in set(tool_recs)))


def memory_checks(ch: dict, mems: list[dict], tools: MovieTools) -> list[str]:
    problems = []
    for kind, want in ch.get("memory_has", {}).items():
        have = [m for m in mems if m["kind"] == kind]
        if isinstance(want, list) and kind in ("avoid_genre", "preference"):
            notes = {(m["note"] or "").lower() for m in have}
            miss = [g for g in want if not any(g.lower() in n for n in notes)]
        elif isinstance(want, list):
            ids = {m["movie_id"] for m in have}
            miss = [t for t in want if _resolve(tools, t) not in ids]
        else:
            miss = [] if len(have) >= want else [f"{want - len(have)} more"]
        if miss:
            problems.append(f"memory lacks {kind}: {miss}")
    for kind, pattern in ch.get("memory_titles_match", {}).items():   # "I've seen Star Wars": only a Star Wars film
        wrong = [tools.data.label(m["movie_id"]) for m in mems if m["kind"] == kind and m["movie_id"] is not None
                 and pattern.lower() not in tools.data.label(m["movie_id"]).lower()]
        if wrong:
            problems.append(f"{kind} memory holds the wrong movie(s): {wrong}")
    for kind, bad in ch.get("memory_lacks", {}).items():
        have = [m for m in mems if m["kind"] == kind]
        if bad == "any" and have:
            problems.append(f"memory should have no {kind}, has {[(m['note'] or tools.data.label(m['movie_id'])) for m in have]}")
        elif isinstance(bad, list):
            for b in bad:
                hit = [m for m in have if (m["note"] and b.lower() in m["note"].lower())
                       or (m["movie_id"] is not None and m["movie_id"] == _resolve(tools, b))]
                if hit:
                    problems.append(f"memory should not have {kind} '{b}'")
    return problems


def check_turn(turn, answer, trace, conv_trace, earlier_recs, tools: MovieTools, user_id: int, scripted: bool,
               history=None, memory_after=None, usage=None, max_call_tokens=None) -> dict:
    called = {t["tool"] for t in trace}
    # a tool already called earlier in this session counts: re-using its output (still in context) is legitimate
    called_session = called | {t["tool"] for t in conv_trace}
    tool_ok = all(any(alt in called_session for alt in req.split("|")) for req in turn["expect_tools"])
    arg_problems = args_check(turn, trace, tools, history)
    arg_problems += [f"must not call {t}" for t in turn.get("forbid_tools", []) if t in called]
    titles = TitleIndex.for_data(tools.data)
    outputs = [t["output"] for t in conv_trace]
    grounding = {k: [] for k in ISSUE_KEYS}
    if not scripted:                          # templates are generated from tool output; nothing to verify
        grounding = check_answer(answer, outputs, titles, user_id)
    mentioned, _ = titles.mentioned(answer)
    tool_recs = recommended_in(trace)
    recs = tool_recs if scripted else [m for m in mentioned if m in set(tool_recs)]
    ch, viol = turn.get("checks", {}), []
    seen = set(tools.data.user_ratings[user_id].index)
    remembered = tools.memory.excluded_movie_ids(user_id)
    excluded_titles = {_resolve(tools, t) for t in ch.get("exclude_titles", [])}
    for m in recs:
        row, label = tools.data.movies.loc[m], tools.data.label(m)
        if ch.get("not_rated") and m in seen:
            viol.append(f"{label}: already rated by user")
        if m in remembered:
            viol.append(f"{label}: remembered as seen/dismissed")
        if m in excluded_titles:
            viol.append(f"{label}: explicitly excluded")
        if ch.get("exclude_genres") and set(ch["exclude_genres"]) & set(row["genres"]):
            viol.append(f"{label}: has excluded genre")
        if ch.get("include_genres") and not set(ch["include_genres"]) & set(row["genres"]):
            viol.append(f"{label}: missing required genre")
        if ch.get("max_year") and row["year"] > ch["max_year"]:
            viol.append(f"{label}: too recent")
        if ch.get("min_year") and row["year"] < ch["min_year"]:
            viol.append(f"{label}: too old")
        if ch.get("no_repeats") and m in earlier_recs:
            viol.append(f"{label}: repeated")
    golden = None
    if turn.get("golden_any"):
        gold = {_resolve(tools, t) for t in turn["golden_any"]}
        golden = bool(gold & set(recs))
    text_ok = True
    low = answer.lower()
    if ch.get("mentions_absent"):
        text_ok = any(p in low for p in ABSENT_PHRASES)
    if ch.get("mentions_any"):
        text_ok = text_ok and any(s.lower() in low for s in ch["mentions_any"])
    if ch.get("mentions_prev") and not (scripted and turn.get("llm_only")):   # needs a model to answer from history
        t, k = ch["mentions_prev"]
        listed = history[t]["recs_order"] if history and t < len(history) else []
        text_ok = text_ok and k < len(listed) and listed[k] in set(TitleIndex.for_data(tools.data).mentioned(answer)[0])
    if ch.get("min_recs"):
        text_ok = text_ok and len(recs) >= ch["min_recs"]
    mem_problems = memory_checks(ch, memory_after if memory_after is not None else tools.memory.list(user_id), tools)
    # context budget per LLM call (what the model sees at once); a turn may make several calls
    budget = turn.get("max_input_tokens")
    if budget and max_call_tokens and max_call_tokens > budget:
        mem_problems.append(f"context budget: a call saw {max_call_tokens} input tokens > {budget}")
    passed = (tool_ok and not arg_problems and not viol and text_ok and golden is not False and not mem_problems
              and not any(grounding.get(k) for k in ISSUE_KEYS))
    return {"passed": passed, "tools_called": sorted(called), "tool_recall_ok": tool_ok, "arg_problems": arg_problems,
            "tool_errors": [t["tool"] for t in trace if t["is_error"]],
            "n_decimals": grounding.get("n_decimals", 0), "n_rating_claims": grounding.get("n_rating_claims", 0),
            **{k: grounding.get(k, []) for k in ISSUE_KEYS},
            "recommended": [tools.data.label(m) for m in recs], "constraint_violations": viol,
            "golden_hit": golden, "text_check_ok": text_ok, "memory_problems": mem_problems}


# ------------------------------------------------------------------ runners
def turn_user(scenario, turn) -> int:
    return turn.get("user_id", scenario["user_id"])


def run_scripted(tools: MovieTools, scenario) -> list[dict]:
    tools.set_user(scenario["user_id"])
    turns, first_rec, current, recs_by_turn = [], None, scenario["user_id"], []
    for turn in scenario["turns"]:
        if turn.get("new_session") or turn_user(scenario, turn) != current:
            current = turn_user(scenario, turn)
            tools.set_user(current)
        start = len(tools.trace)
        parts = []
        for name, args in turn["plan"]:
            def sub(v):
                if v == "$FIRST_REC":
                    return first_rec
                if isinstance(v, str) and v.startswith("$PREV:"):    # "$PREV:t:k" = k-th rec of turn t
                    _, t, k = v.split(":")
                    return str(recs_by_turn[int(t)][int(k)])
                return v
            args = {k: sub(v) for k, v in args.items()}
            text, _ = tools.call(name, args)
            parts.append(render(name, json.loads(text)))
        trace = tools.trace[start:]
        recs = recommended_in(trace)
        recs_by_turn.append(recs)
        if recs:
            first_rec = str(recs[0])
        turns.append({"q": turn["q"], "answer": "\n\n".join(parts), "trace": trace,
                      "memory_after": tools.memory.list(current)})
    return turns


def run_llm(tools: MovieTools, scenario) -> list[dict]:
    from movie_agent.agent import MovieAgent
    agent = MovieAgent(tools=tools)
    RUN_INFO.update(provider=agent.provider, model=agent.model)
    agent.start(scenario["user_id"])
    turns, current = [], scenario["user_id"]
    for turn in scenario["turns"]:
        if turn.get("new_session") or turn_user(scenario, turn) != current:
            current = turn_user(scenario, turn)          # a later visit (or another user): fresh conversation,
            agent.start(current)                         # same long-term memory store
        res = agent.ask(turn["q"])
        turns.append({"q": turn["q"], "answer": res.text, "trace": res.tool_calls, "latency_s": res.latency_s,
                      "ttft_s": res.ttft_s, "usage": res.usage, "revised": res.revised,
                      "guardrail_first": res.guardrail.get("first"), "memory_after": tools.memory.list(current),
                      "context_messages": len(agent.messages),
                      "max_call_input_tokens": max((c.get("input_tokens", 0) for c in res.call_log), default=0)})
    return turns


JUDGE_RUBRIC = """You are grading a movie-recommendation assistant's reply. You get the user's message, the \
assistant's reply, and the JSON tool outputs the assistant saw (ground truth). Score 1-5 each:
- grounded: every claim about ratings, users, counts and movies is supported by the tool outputs
- personalised: the reply uses this user's own history/taste, not generic popularity
- explains: the reasoning is specific and understandable (cites concrete movies/numbers)
- honest: uncertainty, thin evidence, or missing titles are acknowledged where relevant
- helpful: it answers what was asked, respecting the user's constraints
Give a one-sentence rationale naming the biggest weakness."""
JUDGE_KEYS = ["grounded", "personalised", "explains", "honest", "helpful"]


def judge(turn_record: dict) -> dict:
    """LLM-as-judge on one turn. Uses the same provider as the agent (a self-judging bias worth noting)."""
    from movie_agent.agent import detect_provider
    tool_json = json.dumps([{"tool": t["tool"], "output": t["output"]} for t in turn_record["trace"]],
                           ensure_ascii=False)[:60000]
    prompt = f"USER: {turn_record['q']}\n\nREPLY:\n{turn_record['answer']}\n\nTOOL OUTPUTS:\n{tool_json}"
    if detect_provider() == "openai":
        import openai
        r = openai.OpenAI(timeout=60).chat.completions.create(
            model=os.environ.get("MOVIE_AGENT_JUDGE_MODEL", "gpt-4o-mini"), temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": JUDGE_RUBRIC + "\nReply as JSON with integer keys "
                       + ", ".join(JUDGE_KEYS) + " and a string key rationale."},
                      {"role": "user", "content": prompt}])
        return json.loads(r.choices[0].message.content)
    import anthropic
    schema = {"type": "object", "additionalProperties": False, "required": JUDGE_KEYS + ["rationale"],
              "properties": {k: {"type": "integer"} for k in JUDGE_KEYS} | {"rationale": {"type": "string"}}}
    resp = anthropic.Anthropic().messages.create(
        model=config.DEFAULT_MODEL, max_tokens=2000, system=JUDGE_RUBRIC,
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "low"},
        messages=[{"role": "user", "content": prompt}])
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def to_markdown(sc, turns, checks) -> str:
    md = [f"# {sc['id']} (user {sc['user_id']})", ""]
    for turn_def, t, c in zip(sc["turns"], turns, checks):
        if turn_def.get("new_session"):
            md += ["---", "*(new session - long-term memory carries over)*", ""]
        md += [f"**User:** {t['q']}", "", "<details><summary>Tool calls: " + ", ".join(
            f"{x['tool']}({json.dumps(x['input'], ensure_ascii=False)})" for x in t["trace"]) + "</summary>", ""]
        for x in t["trace"]:
            md += [f"`{x['tool']}` ({x['ms']} ms) ->", "```json",
                   json.dumps(x["output"], ensure_ascii=False, indent=1)[:2500], "```"]
        issues = {k: c[k] for k in ("arg_problems", "constraint_violations", "memory_problems", *ISSUE_KEYS) if c[k]}
        md += ["</details>", "", "**Assistant:**", "", t["answer"], "",
               f"> {'PASS' if c['passed'] else 'FAIL'} · tools_ok={c['tool_recall_ok']} · golden={c['golden_hit']} · "
               f"text_ok={c['text_check_ok']} · memory={c.get('memory_after')} · issues={issues or 'none'}"
               + (f" · judge={c['judge']}" if "judge" in c else ""), ""]
    return "\n".join(md)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scripted", "llm"], default="scripted")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--only", help="run a single scenario id")
    ap.add_argument("--repeat", type=int, default=1, help="run each scenario N times (LLM variance)")
    ap.add_argument("--suffix", default="", help="suffix for the output json/dir, e.g. _before_fix")
    ap.add_argument("--suite", choices=["main", "memory", "heldout"], default="main")
    args = ap.parse_args()
    scenarios = SCENARIOS
    if args.suite == "memory":
        from eval.memory_scenarios import MEMORY_SCENARIOS
        scenarios = MEMORY_SCENARIOS
        args.suffix = "_memory" + args.suffix
    if args.suite == "heldout":
        if args.mode != "llm":
            sys.exit("The held-out suite has no reference plans: run it with --mode llm.")
        from eval.heldout_scenarios import HELDOUT_SCENARIOS
        scenarios = HELDOUT_SCENARIOS
        args.suffix = "_heldout" + args.suffix

    tools = MovieTools.build()
    tag = args.mode
    if args.mode == "llm":
        from movie_agent.agent import BACKENDS, detect_provider
        tag = f"llm_{os.environ.get('MOVIE_AGENT_MODEL') or BACKENDS[detect_provider()].default_model}"
    tag += args.suffix
    out_dir = config.OUTPUT_DIR / f"transcripts_{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    scripted = args.mode == "scripted"
    summary = []
    runs = [(sc, r) for sc in scenarios if not args.only or sc["id"] == args.only for r in range(args.repeat)]
    for sc, rep in runs:
        tools.trace = []
        tools.memory = MemoryStore(":memory:")          # isolated long-term memory per run
        turns = run_scripted(tools, sc) if scripted else run_llm(tools, sc)
        checks, earlier, conv_trace, history = [], set(), [], []
        prev_user = sc["user_id"]
        for turn, rec in zip(sc["turns"], turns):
            uid = turn_user(sc, turn)
            if turn.get("new_session") or uid != prev_user:
                conv_trace, earlier = [], set()
            prev_user = uid
            conv_trace += rec["trace"]
            c = check_turn(turn, rec["answer"], rec["trace"], conv_trace, earlier, tools, uid, scripted,
                           history=history, memory_after=rec.get("memory_after"), usage=rec.get("usage"),
                           max_call_tokens=rec.get("max_call_input_tokens"))
            history.append({"recs_order": ordered_recs(rec["answer"], rec["trace"], tools, scripted)})
            if args.judge and not scripted:
                c["judge"] = judge(rec)
            earlier |= set(recommended_in(rec["trace"]))
            c.update(latency_s=rec.get("latency_s"), ttft_s=rec.get("ttft_s"), usage=rec.get("usage"),
                     guardrail_revised=rec.get("revised"), context_messages=rec.get("context_messages"),
                     max_call_input_tokens=rec.get("max_call_input_tokens"),
                     memory_after=[(m["kind"], m["note"] or tools.data.label(m["movie_id"])) for m in rec.get("memory_after", [])])
            checks.append(c)
        name = sc["id"] + (f"_r{rep + 1}" if args.repeat > 1 else "")
        (out_dir / f"{name}.md").write_text(to_markdown(sc, turns, checks), encoding="utf-8")
        summary.append({"id": sc["id"], "run": rep + 1, "user_id": sc["user_id"],
                        "turns": [{"q": t["q"], **c} for t, c in zip(sc["turns"], checks)]})
        print("PASS" if all(c["passed"] for c in checks) else "FAIL", name,
              [("ok" if c["passed"] else {k: c[k] for k in ("arg_problems", "constraint_violations", "golden_hit",
                                                            "text_check_ok", "memory_problems", *ISSUE_KEYS)
                                          if c[k] not in ([], True, None)})
               for c in checks])

    all_turns = [t for s in summary for t in s["turns"]]
    n = len(all_turns)
    golden = [t["golden_hit"] for t in all_turns if t["golden_hit"] is not None]
    by_scn = defaultdict(list)
    for s in summary:
        by_scn[s["id"]].append(all(t["passed"] for t in s["turns"]))
    agg = {
        "mode": args.mode, **RUN_INFO, "repeat": args.repeat, "n_scenario_runs": len(summary), "n_turns": n,
        "turn_pass_rate": round(sum(t["passed"] for t in all_turns) / n, 3),
        "scenario_pass_rate": round(sum(all(t["passed"] for t in s["turns"]) for s in summary) / len(summary), 3),
        "tool_recall": round(sum(t["tool_recall_ok"] for t in all_turns) / n, 3),
        "args_ok": round(sum(not t["arg_problems"] for t in all_turns) / n, 3),
        "turns_with_constraint_violation": sum(bool(t["constraint_violations"]) for t in all_turns),
        "golden_hit_rate": round(sum(golden) / len(golden), 3) if golden else None,
        "text_checks_passed": round(sum(t["text_check_ok"] for t in all_turns) / n, 3),
        **{k: sum(len(t[k]) for t in all_turns) for k in ISSUE_KEYS},
        "decimal_claims_checked": sum(t["n_decimals"] for t in all_turns),
        "rating_claims_checked": sum(t["n_rating_claims"] for t in all_turns),
        "tool_errors": sum(len(t["tool_errors"]) for t in all_turns),
        "online_guardrail_revisions": sum(bool(t.get("guardrail_revised")) for t in all_turns),
        "per_scenario_pass": {k: f"{sum(v)}/{len(v)}" for k, v in by_scn.items()},
    }
    if args.judge and not scripted:
        for k in JUDGE_KEYS:
            agg[f"judge_{k}"] = round(sum(t["judge"].get(k, 0) for t in all_turns) / n, 2)
    lat = sorted(t["latency_s"] for t in all_turns if t.get("latency_s"))
    if lat:
        agg["latency_p50_s"] = lat[len(lat) // 2]
        agg["latency_p95_s"] = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
    ttft = sorted(t["ttft_s"] for t in all_turns if t.get("ttft_s"))
    if ttft:
        agg["ttft_p50_s"] = ttft[len(ttft) // 2]
    (config.OUTPUT_DIR / "eval").mkdir(parents=True, exist_ok=True)
    (config.OUTPUT_DIR / "eval" / f"scenarios_{tag}.json").write_text(
        json.dumps({"aggregate": agg, "scenarios": summary}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(agg, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
