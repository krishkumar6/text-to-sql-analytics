"""Pipeline tests against the real Postgres container, with a scripted LLM."""

import pytest

from app.db import QueryError
from app.llm import LLMError
from tests.conftest import draft, summary, text_response, tool_use_response

PLAN_MRR = """
SELECT p.name AS plan, round(sum(s.mrr), 2) AS mrr
FROM subscriptions s JOIN plans p USING (plan_id)
WHERE s.status IN ('active', 'past_due') AND p.monthly_price > 0
GROUP BY p.name, p.tier_rank ORDER BY p.tier_rank
"""


def test_happy_path(pipeline, fake_client, traces):
    fake_client.queue(draft(PLAN_MRR), summary("Pro leads MRR.", "bar", "plan", ["mrr"]))

    out = pipeline.ask("What is current MRR by plan?")

    assert out.status == "ok" and out.attempts == 1
    assert out.columns == ["plan", "mrr"]
    assert [r[0] for r in out.rows] == ["Starter", "Pro", "Business"]
    assert out.summary == "Pro leads MRR."
    assert out.chart["data"][0]["type"] == "bar"
    assert "LIMIT" in out.sql  # guard added the row cap
    assert set(out.step_latency_ms) >= {"generate", "validate", "execute", "summarize"}

    trace = next(t for t in traces.recent(20) if t["trace_id"] == out.trace_id)
    assert trace["status"] == "ok" and trace["row_count"] == 3 and trace["input_tokens"] == 200


def test_database_error_is_sent_back_and_repaired_once(pipeline, fake_client):
    fake_client.queue(
        draft("SELECT plan_name FROM plans"),  # no such column
        draft("SELECT name FROM plans ORDER BY tier_rank"),
        summary(),
    )

    out = pipeline.ask("List the plans")

    assert out.status == "ok" and out.attempts == 2 and out.row_count == 4
    repair_request = fake_client.messages.calls[1]["messages"][-1]["content"]
    assert 'column "plan_name" does not exist' in repair_request


def test_gives_up_after_one_retry(pipeline, fake_client):
    fake_client.queue(draft("DELETE FROM users"), draft("DROP TABLE users"))

    out = pipeline.ask("Remove all users")

    assert out.status == "rejected" and out.attempts == 2
    assert "Only read-only SELECT" in out.error
    assert len(fake_client.messages.calls) == 2  # no summary call for a failed query


def test_clarification_skips_execution(pipeline, fake_client):
    fake_client.queue(draft(clarify="Do you mean revenue or MRR?"))

    out = pipeline.ask("How much money did we make?")

    assert out.status == "clarification"
    assert out.clarifying_question == "Do you mean revenue or MRR?"
    assert out.rows == [] and out.attempts == 0


def test_lookup_tool_returns_real_values(pipeline, fake_client):
    fake_client.queue(
        tool_use_response({"table": "users", "column": "country", "search": "united"}),
        draft("SELECT count(*) AS users FROM users WHERE country = 'United Kingdom'"),
        summary(),
    )

    out = pipeline.ask("How many users in the UK?")

    assert out.status == "ok"
    tool_result = fake_client.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is False
    assert "United Kingdom" in tool_result["content"] and "United States" in tool_result["content"]


def test_lookup_tool_rejects_unknown_columns(pipeline, fake_client):
    fake_client.queue(
        tool_use_response({"table": "users", "column": "password", "search": ""}),
        draft("SELECT count(*) FROM users"),
        summary(),
    )
    pipeline.ask("How many users?")
    tool_result = fake_client.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True


def test_summary_failure_still_returns_data(pipeline, fake_client):
    fake_client.queue(draft(PLAN_MRR), text_response({}, stop_reason="refusal"))

    out = pipeline.ask("What is current MRR by plan?")

    assert out.status == "ok" and out.summary is None
    assert out.chart is not None  # heuristic chart fallback


def test_llm_refusal_is_raised_and_traced(pipeline, fake_client, traces):
    fake_client.queue(text_response({}, stop_reason="refusal"))
    with pytest.raises(LLMError):
        pipeline.ask("something")
    assert traces.recent(1)[0]["status"] == "error"


def test_edited_sql_goes_through_the_guard(pipeline, fake_client):
    out = pipeline.run_sql("UPDATE plans SET monthly_price = 0")
    assert out.status == "rejected"
    assert fake_client.messages.calls == []


def test_read_only_role_blocks_writes_even_without_the_guard(db):
    # The guard is defence in depth; the database role is the real boundary.
    with pytest.raises(QueryError, match="read-only transaction|permission denied"):
        db.execute("DELETE FROM users")
    with pytest.raises(QueryError, match="permission denied"):
        db.execute("SELECT * FROM ops.query_traces")


def test_statement_timeout(db):
    original = db.statement_timeout_ms
    db.statement_timeout_ms = 100
    try:
        with pytest.raises(QueryError, match="statement timeout"):
            db.execute("SELECT pg_sleep(2)")
    finally:
        db.statement_timeout_ms = original


def test_row_cap(db):
    result = db.execute("SELECT event_id FROM events", max_rows=10)
    assert result.row_count == 10 and result.truncated
