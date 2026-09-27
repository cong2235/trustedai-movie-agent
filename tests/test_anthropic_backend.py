"""Offline tests for the Claude (Anthropic) backend, with a fake client.

The reported runs used OpenAI; no Anthropic key was available during development. These tests pin down what the
Claude path must do on the Messages API: send the tools and a cached system prompt, return every tool result in one
user message with matching tool_use ids, drive a full agent turn, handle a refusal, and fall back when the beta
`fallbacks` parameter is rejected.
"""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.cf import CFModel  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.memory import MemoryStore  # noqa: E402
from movie_agent.recommender import Recommender  # noqa: E402
from movie_agent.telemetry import Telemetry  # noqa: E402
from movie_agent.tools import TOOL_SCHEMAS, MovieTools  # noqa: E402


class _BadRequest(Exception):
    pass


def _text(t):
    return NS(type="text", text=t)


def _tool_use(cid, name, args):
    return NS(type="tool_use", id=cid, name=name, input=args)


def _reply(content, stop):
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=100, output_tokens=10))


class _FakeMessages:
    def __init__(self, replies, reject_fallbacks=False):
        self.replies, self.calls, self.reject_fallbacks = list(replies), [], reject_fallbacks

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw.get("messages", []))})
        if self.reject_fallbacks and "extra_body" in kw:
            raise _BadRequest("fallbacks not supported")
        return self.replies.pop(0)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from movie_agent.agent import AnthropicBackend

    be = AnthropicBackend("claude-opus-5")
    be.anthropic = NS(BadRequestError=_BadRequest)
    return be


def test_request_shape_and_tool_calls(backend):
    backend.client = NS(
        messages=_FakeMessages([_reply([_text("Let me look."), _tool_use("tu_1", "get_user_profile", {})], "tool_use")])
    )
    messages = [{"role": "user", "content": "What should I watch?"}]
    text, calls, stop, usage = backend.step("The current user is user_id=15.", messages)
    kw = backend.client.messages.calls[0]
    assert kw["tools"] == TOOL_SCHEMAS and kw["model"] == "claude-opus-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "user_id=15" in kw["system"][1]["text"]
    assert calls == [("tu_1", "get_user_profile", {})] and stop == "tool_use" and usage["input_tokens"] == 100
    assert messages[-1]["role"] == "assistant"


def test_parallel_tool_results_go_in_one_message(backend):
    messages = []
    backend.tool_results(messages, [("tu_1", '{"a": 1}', False), ("tu_2", '{"error": "x"}', True)])
    assert len(messages) == 1 and messages[0]["role"] == "user"
    blocks = messages[0]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["tu_1", "tu_2"]
    assert all(b["type"] == "tool_result" for b in blocks) and blocks[1]["is_error"] is True


def test_rejected_fallbacks_beta_is_switched_off(backend):
    backend.client = NS(messages=_FakeMessages([_reply([_text("hi")], "end_turn")], reject_fallbacks=True))
    text, *_ = backend.step("ctx", [{"role": "user", "content": "hi"}])
    assert text == "hi" and backend.use_fallbacks is False
    assert "extra_body" not in backend.client.messages.calls[-1]


def test_refusal_is_reported_not_passed_through(backend):
    backend.client = NS(messages=_FakeMessages([_reply([], "refusal")]))
    text, calls, stop, _ = backend.step("ctx", [{"role": "user", "content": "..."}])
    assert stop == "refusal" and calls == [] and text.startswith("Sorry")


def test_full_agent_turn_on_claude(tmp_path, monkeypatch):
    """Tool call -> tool result -> grounded final answer, through MovieAgent and the guardrail."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from movie_agent.agent import MovieAgent

    data = MovieData.load()
    cf = CFModel(data)
    tools = MovieTools(data=data, cf=cf, content=None, rec=Recommender(data, cf, None), memory=MemoryStore(":memory:"))
    agent = MovieAgent(
        tools=tools,
        provider="anthropic",
        model="claude-opus-5",
        telemetry=Telemetry(db_path=tmp_path / "t.db", jsonl_path=tmp_path / "a.jsonl"),
    )
    agent.backend.anthropic = NS(BadRequestError=_BadRequest)
    fake = _FakeMessages(
        [
            _reply([_tool_use("tu_1", "similar_users_opinion", {"movie": "Pulp Fiction"})], "tool_use"),
            _reply([_text("You rated **Pulp Fiction (1994)** 3 stars; similar users liked it more.")], "end_turn"),
        ]
    )
    agent.backend.client = NS(messages=fake)
    agent.start(1)
    res = agent.ask("What do people like me think of Pulp Fiction?")
    assert [c["tool"] for c in res.tool_calls] == ["similar_users_opinion"] and not res.tool_calls[0]["is_error"]
    assert "Pulp Fiction (1994)" in res.text and not res.revised
    second_request = fake.calls[1]["messages"]
    assert second_request[-1]["content"][0]["tool_use_id"] == "tu_1"
