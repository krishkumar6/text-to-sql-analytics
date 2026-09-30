"""GroqLLM against a scripted chat-completions client (the draft arrives as a submit_sql_draft tool call)."""

import json
from types import SimpleNamespace

import pytest

from app.llm import GroqLLM, LLMError
from app.pipeline import Pipeline


def _usage():
    return SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)


def tool_calls(*calls, finish_reason="tool_calls"):
    msg = SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)
    ])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)], usage=_usage())


def submit(sql, **extra):
    args = {"sql": sql, "explanation": "e", "confidence": "high", "needs_clarification": False,
            "clarifying_question": "", "assumptions": [], **extra}
    return tool_calls(("submit_sql_draft", args))


def json_reply(payload, finish_reason="stop"):
    msg = SimpleNamespace(content=json.dumps(payload), tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)], usage=_usage())


def summary_reply(text="Summary."):
    return json_reply({"summary": text, "chart": {"kind": "none", "x": "", "y": [], "series": "", "title": ""}})


class FakeGroq:
    def __init__(self, *responses):
        self.script = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)


@pytest.fixture
def groq_pipeline(settings, db, catalog, traces, business_context):
    def make(*responses):
        fake = FakeGroq(*responses)
        return Pipeline(settings, db, catalog, GroqLLM(fake, settings, catalog, db, business_context), traces), fake
    return make


def test_draft_via_submit_tool(groq_pipeline):
    pipeline, fake = groq_pipeline(submit("SELECT name FROM plans ORDER BY tier_rank"), summary_reply("4 plans."))

    out = pipeline.ask("List plans")

    assert out.status == "ok" and out.row_count == 4 and out.summary == "4 plans."
    first = fake.calls[0]
    assert first["tool_choice"] == "required" and "response_format" not in first
    assert first["messages"][0]["role"] == "system"
    assert fake.calls[1]["response_format"]["type"] == "json_schema" and "tools" not in fake.calls[1]


def test_lookup_then_submit(groq_pipeline):
    pipeline, fake = groq_pipeline(
        tool_calls(("lookup_column_values", {"table": "users", "column": "country", "search": "india"})),
        submit("SELECT count(*) AS n FROM users WHERE country = 'India'"),
        summary_reply(),
    )

    out = pipeline.ask("Users in India?")

    assert out.status == "ok"
    tool_msg = fake.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "India" in tool_msg["content"]


def test_repair_answers_pending_tool_calls_first(groq_pipeline):
    pipeline, fake = groq_pipeline(
        submit("SELECT plan_name FROM plans"),
        submit("SELECT name FROM plans"),
        summary_reply(),
    )

    out = pipeline.ask("List plans")

    assert out.status == "ok" and out.attempts == 2
    history = fake.calls[1]["messages"]
    # assistant tool call -> tool result -> user repair request (Groq rejects any other order)
    assert [m["role"] for m in history[-3:]] == ["assistant", "tool", "user"]
    assert 'column "plan_name" does not exist' in history[-1]["content"]


def test_invalid_draft_is_sent_back(groq_pipeline):
    pipeline, fake = groq_pipeline(
        submit("SELECT 1", confidence="certain"),  # not in the enum
        submit("SELECT count(*) AS n FROM plans"),
        summary_reply(),
    )

    out = pipeline.ask("How many plans?")

    assert out.status == "ok" and out.rows == [[4]]
    assert fake.calls[1]["messages"][-1]["content"].startswith("Error: Draft did not match")


def test_truncated_output_raises(groq_pipeline):
    pipeline, _ = groq_pipeline(tool_calls(finish_reason="length"))
    with pytest.raises(LLMError, match="cut off"):
        pipeline.ask("anything")
