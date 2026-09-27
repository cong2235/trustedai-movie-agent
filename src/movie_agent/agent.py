"""LLM agent: the model plans which tools to call, calls them (possibly several rounds, in parallel),
then answers with an explanation grounded in the tool outputs.

Two interchangeable backends share one prompt, one tool list and one loop shape:
  * AnthropicBackend - Claude via the Messages API (default model claude-opus-5)
  * OpenAIBackend    - OpenAI Chat Completions with function tools (default model gpt-4o-mini)
The provider comes from MOVIE_AGENT_PROVIDER, or is auto-detected from which API key is set.

A manual tool-use loop (rather than an SDK tool runner) is used because the evaluation needs the full
trace of every call (inputs, outputs, latency), and the loop is short.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import config
from .guardrails import TitleIndex, check_answer, has_issues, revision_request
from .telemetry import Telemetry, cost_usd
from .tools import TOOL_SCHEMAS, MovieTools

SYSTEM_PROMPT = """You are a movie-discovery assistant working over a fixed MovieLens dataset: 5,135 movies \
(1903-2014) with full plot summaries, 74k ratings from 610 users (0.5-5 stars), and sparse user tags. \
You investigate the data on the user's behalf with the tools, then answer.

How to work
- Think about what evidence the question needs, then gather it. Most good answers combine several signals: \
the user's own ratings, similar users' ratings, plot content, genres, and rating counts. Call independent \
tools in parallel.
- A profile summary of the user (and anything remembered from earlier sessions) is in your context. Use it \
directly; call get_user_profile only when you need more detail than the summary gives.
- Long-term memory: when the user says they have seen a movie, are not interested in one, liked or disliked one, \
or states a lasting preference, call remember (in parallel with your other tools). Respect remembered \
preferences. Movies remembered as seen/dismissed/disliked are already filtered out by the tools. \
When the user names movies they have already watched while asking for recommendations, pass them as \
already_seen to recommend_movies / search_movies: that excludes and remembers them in one call. Never put \
your own earlier suggestions in already_seen or exclude_titles to avoid repeats: the tools never repeat a \
movie within a session.
- One-off constraints ("just for tonight", "this time") apply to the current request only: pass them as tool \
arguments, do not remember them. When the user retracts a stored preference, call forget_memory (by kind and \
genre/movie). Never say you stored or removed something unless the tool call succeeded.
- When the user explicitly asks for a genre, pass exactly that genre in include_genres (it overrides a remembered \
dislike for this request) and do not also put it in exclude_genres.
- For "why would I like X?" use explain_match and cite the concrete movies and numbers it returns. \
Mention counter-evidence when it exists.
- When the user refers to "that" or "the second one", resolve it from the conversation.
- Map the request onto tool arguments fully: a movie the user liked or wants something like goes in more_like, genres to avoid go in exclude_genres, an era goes in min_year/max_year, a mood goes in mood_or_description (search: query) and, when it matches, also in moods / twist_ending / max_violence.

Grounding rules (these matter most)
- Only recommend movies returned by a tool in this conversation. Never recommend from memory: many famous \
movies are absent, and a movie outside the dataset is a hard failure.
- Every factual claim about the user or a movie (ratings, counts, averages, similar users) must come from \
tool output. You may use general film knowledge only for colour (e.g. the director), and must label it as such.
- If a tool says a title is ambiguous, pick the obvious match or ask. If it says a title is absent, say so plainly.
- If a movie is flagged plot_unreliable, do not describe its plot from the data.
- Describe each kind of evidence for what it is. "because_you_rated" is a co-rating pattern (people who rated \
those movies like you did also rated this highly), not a similarity of theme or style: never write that a \
co-rated movie "aligns with its themes" or explains a taste for its humour or story. Only \
"similar_plots_you_liked" supports claims about similar stories.
- Always say whose number it is: a movie's avg_rating is everyone's average ("rated 4.0 on average by \
everyone"), similar_users_who_rated_it.avg_rating is your similar users' ("people with your taste average \
4.5"), and your_rating is the user's own. Never attach everyone's average to "users like you", or the reverse.
- Be honest about thin evidence: surface evidence_strength / reliability when it is weak or moderate, \
e.g. "only 3 similar users rated it", and say so when expected_fit is not "good match".
- Describe filters exactly as applied: min_year=2010 means "from 2010 on", not "after 2010".
- Respect every constraint the user gave (genres to avoid, era, "not seen before"). Check the results against \
the constraints before answering.

Style
- Reply in the language of the user's latest message (a question in Vietnamese gets a Vietnamese answer), \
including your explanations. Keep movie titles exactly as the tools return them, as "Title (Year)".
- Lead with the answer. Write every movie as "Title (Year)". For each recommendation give one or two \
sentences of evidence-based reasoning in plain language, e.g. "you gave Aliens (1986) 5 stars and people \
who rated both liked this". Avoid raw jargon such as z-scores or item_knn.
- Keep it scannable: usually 3-5 recommendations, short paragraphs or a compact list.
- End with at most one short follow-up suggestion when it helps."""


def detect_provider() -> str | None:
    explicit = os.environ.get("MOVIE_AGENT_PROVIDER", "").lower()
    if explicit in ("anthropic", "openai"):
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


@dataclass
class TurnResult:
    text: str
    tool_calls: list[dict]
    latency_s: float
    usage: dict
    stop_reason: str
    turn_id: str = ""
    guardrail: dict = field(default_factory=dict)
    revised: bool = False
    ttft_s: float | None = None  # time to first streamed answer token
    call_log: list = field(default_factory=list)  # per LLM call: latency, input tokens, hedged


# --------------------------------------------------------------------------- backends
class AnthropicBackend:
    default_model = config.DEFAULT_MODEL

    def __init__(self, model: str):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        # Server-side refusal fallback (beta), sent via extra_body because this SDK version has no typed
        # parameter for it. It switches itself off if the API rejects it.
        self.use_fallbacks = os.environ.get("MOVIE_AGENT_FALLBACKS", "1") == "1"

    def _create(self, **kwargs):
        if self.use_fallbacks:
            try:
                return self.client.messages.create(
                    **kwargs,
                    extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                    extra_body={"fallbacks": "default"},
                )
            except self.anthropic.BadRequestError:
                self.use_fallbacks = False
        return self.client.messages.create(**kwargs)

    def step(self, system: str, messages: list, on_token=None) -> tuple[str, list[tuple[str, str, dict]], str, dict]:
        """One model call. Returns (text, [(call_id, tool_name, args)], stop_reason, usage) and appends the reply.
        (Streaming is implemented on the OpenAI backend; this backend returns the whole reply at once.)"""
        r = self._create(
            model=self.model,
            max_tokens=16000,
            tools=TOOL_SCHEMAS,
            messages=messages,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system},
            ],
        )
        messages.append({"role": "assistant", "content": r.content})
        calls = [(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in r.content if b.type == "text").strip()
        if r.stop_reason == "refusal":
            text = "Sorry - I can't help with that request."
        usage = {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}
        return text, calls, r.stop_reason, usage

    def tool_results(self, messages: list, results: list[tuple[str, str, bool]]) -> None:
        messages.append(
            {
                "role": "user",
                "content": [  # all results in ONE message keeps parallel calls working
                    {"type": "tool_result", "tool_use_id": cid, "content": text, "is_error": err}
                    for cid, text, err in results
                ],
            }
        )

    def user(self, messages: list, text: str) -> None:
        messages.append({"role": "user", "content": text})

    def assistant(self, messages: list, text: str) -> None:
        messages.append({"role": "assistant", "content": text or "(no answer)"})


class OpenAIBackend:
    default_model = "gpt-4o-mini"

    def __init__(self, model: str):
        import openai

        from .http import openai_client

        self.client = openai_client(
            timeout=30.0, max_retries=2
        )  # shared warm pool; bound the tail (SDK default 10 min)
        self._hedge_client = openai.OpenAI(timeout=30.0, max_retries=0)
        self.model = model
        self.tools = [
            {
                "type": "function",
                "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
            }
            for t in TOOL_SCHEMAS
        ]

    def step(self, system: str, messages: list, on_token=None):
        """One streamed model call. Text deltas go to on_token as they arrive (time-to-first-token is what the
        user feels); tool-call deltas are accumulated by index. Returns (text, calls, stop_reason, usage)."""
        msgs = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + system}] + messages
        t_call = time.time()
        stream, hedged, first_chunk_s = self._hedged_stream(
            dict(
                model=self.model,
                messages=msgs,
                tools=self.tools,
                temperature=0.2,
                max_completion_tokens=4000,
                stream=True,
                stream_options={"include_usage": True},
            )
        )
        text, tool_acc, finish, usage = [], {}, None, None
        for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            delta = ch.delta
            if delta.content:
                text.append(delta.content)
                if on_token:
                    on_token(delta.content)
            for tc in delta.tool_calls or []:
                acc = tool_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                acc["id"] = tc.id or acc["id"]
                if tc.function is not None:
                    acc["name"] += tc.function.name or ""
                    acc["args"] += tc.function.arguments or ""
            finish = ch.finish_reason or finish
        content = "".join(text)
        entry = {"role": "assistant", "content": content}
        calls = []
        if tool_acc:
            entry["tool_calls"] = [
                {"id": a["id"], "type": "function", "function": {"name": a["name"], "arguments": a["args"]}}
                for _, a in sorted(tool_acc.items())
            ]
            for _, a in sorted(tool_acc.items()):
                try:
                    args = json.loads(a["args"] or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": a["args"]}
                calls.append((a["id"], a["name"], args))
        messages.append(entry)
        stop = "tool_use" if calls else (finish or "stop")
        u = {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "call": {
                "first_chunk_s": round(first_chunk_s, 2),
                "total_s": round(time.time() - t_call, 2),
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "hedged": hedged,
                "tool_calls": len(calls),
            },
        }
        return content.strip(), calls, stop, u

    def _hedged_stream(self, kwargs):
        """Tail-latency hedging. Identical prompts to gpt-4o-mini sometimes take 10-12 s instead of ~2.5 s, almost
        all of it before the first chunk. If no chunk has arrived after HEDGE_AFTER_S, send the same request again
        and stream whichever answers first; the loser is closed. Costs a duplicate call only on slow requests.
        Returns (chunk iterator, hedged?, seconds to first chunk)."""
        import queue
        import threading

        t0 = time.time()
        q: queue.Queue = queue.Queue()
        stop = [threading.Event(), threading.Event()]

        def worker(i: int) -> None:
            try:
                # the duplicate goes through its own client / connection pool, so a stalled connection
                # cannot hold both requests
                client = self.client if i == 0 else self._hedge_client
                st = client.chat.completions.create(**kwargs)
                for ch in st:
                    if stop[i].is_set():
                        st.close()
                        return
                    q.put((i, ch))
                q.put((i, None))  # end of stream
            except Exception as e:  # surfaced only if it is the stream we end up using
                q.put((i, e))

        threading.Thread(target=worker, args=(0,), daemon=True).start()
        hedged = config.HEDGE_AFTER_S <= 0  # disabled: behave as a plain stream (never duplicate)
        errors = {}
        while True:
            try:
                i, first = q.get(timeout=None if hedged else config.HEDGE_AFTER_S)
            except queue.Empty:
                hedged = True
                threading.Thread(target=worker, args=(1,), daemon=True).start()
                continue
            if isinstance(first, Exception):
                errors[i] = first
                if config.HEDGE_AFTER_S <= 0 or not hedged or len(errors) == 2:
                    raise first
                continue  # one request failed; wait for the other
            winner = i
            break
        stop[1 - winner].set()
        first_chunk_s = time.time() - t0

        def chunks():
            item = first
            while item is not None:
                if isinstance(item, Exception):
                    raise item
                yield item
                j, item = q.get()
                while j != winner:  # drop anything the loser already queued
                    j, item = q.get()

        return chunks(), hedged and config.HEDGE_AFTER_S > 0, first_chunk_s

    def tool_results(self, messages: list, results) -> None:
        for cid, text, _ in results:
            messages.append({"role": "tool", "tool_call_id": cid, "content": text})

    def user(self, messages: list, text: str) -> None:
        messages.append({"role": "user", "content": text})

    def assistant(self, messages: list, text: str) -> None:
        messages.append({"role": "assistant", "content": text or "(no answer)"})


BACKENDS = {"anthropic": AnthropicBackend, "openai": OpenAIBackend}


# --------------------------------------------------------------------------- agent
@dataclass
class MovieAgent:
    tools: MovieTools
    provider: str | None = None
    model: str | None = None
    messages: list = field(default_factory=list)
    telemetry: Telemetry | None = None
    guardrail: bool = True  # re-check every answer against the tool outputs; revise once on failure

    def __post_init__(self):
        self.provider = self.provider or detect_provider()
        if self.provider not in BACKENDS:
            raise RuntimeError(
                "No LLM provider configured: set ANTHROPIC_API_KEY or OPENAI_API_KEY (see .env.example)."
            )
        cls = BACKENDS[self.provider]
        self.model = self.model or os.environ.get("MOVIE_AGENT_MODEL") or cls.default_model
        self.backend = cls(self.model)
        self.telemetry = self.telemetry or Telemetry(enabled=os.environ.get("MOVIE_AGENT_TELEMETRY", "1") == "1")
        self.titles = TitleIndex.for_data(self.tools.data)
        self.session_id = Telemetry.new_id()
        self._session_trace_start = 0
        self._turns: list[dict] = []  # per turn: index of its first message, question, final answer
        self._call_log: list[dict] = []  # per LLM call of the current turn (reset in ask)

    def start(self, user_id: int) -> dict:
        self.messages = []
        self._turns = []
        self.session_id = Telemetry.new_id()
        self._session_trace_start = len(self.tools.trace)
        return self.tools.set_user(user_id)

    def _context(self) -> str:
        uid = self.tools.session.user_id
        if uid is None:
            return "No user identified yet; ask for their user ID (1-610) before personalising."
        parts = [f"The current user is user_id={uid}. Tools default to this user.", self.tools.session.profile_summary]
        mem = self.tools.memory_context()
        if mem:
            parts.append(mem)
        return "\n".join(p for p in parts if p)

    def _compact(self) -> None:
        """Short-term memory management: keep the last KEEP_FULL_TURNS turns verbatim (tool calls and outputs
        included) and reduce older turns to (question, final answer). Old tool outputs are the bulk of the
        context and are rarely needed again; what was already suggested is tracked in the session anyway."""
        keep = config.KEEP_FULL_TURNS
        if len(self._turns) <= keep:
            return
        old, recent = self._turns[:-keep], self._turns[-keep:]
        if all(t.get("compacted") for t in old):
            return
        new: list = []
        for t in old:
            t["start"] = len(new)
            self.backend.user(new, t["question"])
            self.backend.assistant(new, t["answer"])
            t["compacted"] = True
        offset = recent[0]["start"]
        tail = self.messages[offset:]
        for t in recent:
            t["start"] = t["start"] - offset + len(new)
        self.messages = new + tail

    def _run_loop(self, usage: dict, counter: list, on_token=None) -> tuple[str, str]:
        """Model <-> tools until the model answers without calling tools (or the step budget runs out)."""
        text, stop = "", ""
        for step in range(config.MAX_AGENT_STEPS + 1):
            if step == config.MAX_AGENT_STEPS:
                self.backend.user(self.messages, "Tool budget reached - answer now with what you have.")
            text, calls, stop, u = self.backend.step(self._context(), self.messages, on_token=on_token)
            counter[0] += 1
            for k in ("input_tokens", "output_tokens"):
                usage[k] += u.get(k, 0) or 0
            if "call" in u:
                self._call_log.append(u["call"])
            if not calls or step == config.MAX_AGENT_STEPS:
                break
            results = []
            for cid, name, args in calls:
                out, is_error = self.tools.call(name, args)
                results.append((cid, out, is_error))
            self.backend.tool_results(self.messages, results)
        return text, stop

    def ask(self, user_text: str, on_token=None) -> TurnResult:
        """on_token(str) receives answer text as it streams (OpenAI backend). If the grounding check then
        forces a revision, TurnResult.revised is True and TurnResult.text is the corrected answer."""
        t0 = time.time()
        turn_id = Telemetry.new_id()
        trace_start = len(self.tools.trace)
        usage, llm_calls = {"input_tokens": 0, "output_tokens": 0}, [0]
        report, revised, after, error = {}, False, None, None
        text, stop = "", "error"
        first_token = [None]
        self._call_log = []

        def _tok(delta: str) -> None:
            if first_token[0] is None:
                first_token[0] = time.time() - t0
            if on_token:
                on_token(delta)

        try:
            self._compact()
            self._turns.append({"start": len(self.messages), "question": user_text, "answer": ""})
            self.tools.session.last_user_message = user_text
            self.backend.user(self.messages, user_text)
            text, stop = self._run_loop(usage, llm_calls, on_token=_tok)
            if self.guardrail and text:
                outputs = [t["output"] for t in self.tools.trace[self._session_trace_start :]]
                report = check_answer(text, outputs, self.titles, self.tools.session.user_id)
                if has_issues(report):
                    revised = True
                    self.backend.user(self.messages, revision_request(report))
                    text, stop = self._run_loop(usage, llm_calls)  # revision is not streamed
                    outputs = [t["output"] for t in self.tools.trace[self._session_trace_start :]]
                    after = check_answer(text, outputs, self.titles, self.tools.session.user_id)
            self._turns[-1]["answer"] = text
        except Exception as e:  # log, then surface to the caller
            error = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            calls = self.tools.trace[trace_start:]
            latency = time.time() - t0
            self.telemetry.log_turn(
                {
                    "turn_id": turn_id,
                    "session_id": self.session_id,
                    "user_id": self.tools.session.user_id,
                    "provider": self.provider,
                    "model": self.model,
                    "question": user_text,
                    "answer": text,
                    "latency_ms": round(latency * 1000),
                    "llm_calls": llm_calls[0],
                    **usage,
                    "cost_usd": cost_usd(self.model, usage["input_tokens"], usage["output_tokens"]),
                    "n_tool_calls": len(calls),
                    "n_tool_errors": sum(c["is_error"] for c in calls),
                    "stop_reason": stop,
                    "guardrail_issues": int(has_issues(report)) if report else 0,
                    "guardrail_detail": json.dumps({"first": report, "after_revision": after}, ensure_ascii=False),
                    "revised": int(revised),
                    "issues_after_revision": None if after is None else int(has_issues(after)),
                    "ttft_ms": None if first_token[0] is None else round(first_token[0] * 1000),
                    "context_messages": len(self.messages),
                    "llm_call_detail": json.dumps(self._call_log),
                    "error": error,
                },
                calls,
            )
        return TurnResult(
            text=text,
            tool_calls=calls,
            latency_s=round(latency, 1),
            usage=usage,
            stop_reason=stop,
            turn_id=turn_id,
            guardrail={"first": report, "after_revision": after},
            revised=revised,
            ttft_s=None if first_token[0] is None else round(first_token[0], 2),
            call_log=list(self._call_log),
        )
