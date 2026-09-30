import pytest
from fastapi.testclient import TestClient

from app.llm import AnthropicLLM, GroqLLM
from app.main import app
from tests.conftest import FakeClient, draft, summary


def swap_llm(llm_cls, client):
    p = app.state.pipeline
    p.llm = llm_cls(client, p.settings, p.catalog, p.db, p.settings.business_context_path.read_text("utf-8"))


@pytest.fixture
def client(db):  # `db` skips these tests when Postgres isn't running
    with TestClient(app) as test_client:
        fake = FakeClient()
        swap_llm(AnthropicLLM, fake)
        test_client.fake = fake
        yield test_client


def test_tables(client):
    names = {t["name"] for t in client.get("/tables").json()}
    assert names == {"users", "plans", "subscriptions", "orders", "events"}


def test_ask_then_feedback(client):
    client.fake.queue(draft("SELECT name, monthly_price FROM plans ORDER BY tier_rank"),
                      summary("Four plans.", "bar", "name", ["monthly_price"]))

    body = client.post("/ask", json={"question": "List plans and prices"}).json()

    assert body["status"] == "ok" and body["row_count"] == 4 and body["chart"]
    assert body["usage"]["llm_calls"] == 2
    fb = client.post("/feedback", json={"trace_id": body["trace_id"], "rating": -1, "comment": "wrong prices"})
    assert fb.status_code == 200
    trace = next(t for t in client.get("/traces").json() if t["trace_id"] == body["trace_id"])
    assert trace["feedback"] == -1 and trace["feedback_comment"] == "wrong prices"


def test_run_edited_sql(client):
    client.fake.queue(summary("There are 4,000 users."))
    body = client.post("/run", json={"sql": "SELECT count(*) AS n FROM users"}).json()
    assert body["status"] == "ok" and body["rows"] == [[4000]]
    assert body["summary"] == "There are 4,000 users."


def test_feedback_for_unknown_trace(client):
    res = client.post("/feedback", json={"trace_id": "00000000-0000-0000-0000-000000000000", "rating": 1})
    assert res.status_code == 404


@pytest.mark.parametrize("provider", ["anthropic", "groq"])
def test_missing_credentials(client, monkeypatch, provider):
    if provider == "anthropic":
        import anthropic

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        swap_llm(AnthropicLLM, anthropic.Anthropic(api_key=None, auth_token=None))
        key_name = "ANTHROPIC_API_KEY"
    else:
        swap_llm(GroqLLM, None)  # create_llm passes None when GROQ_API_KEY is unset
        key_name = "GROQ_API_KEY"

    ask = client.post("/ask", json={"question": "What is current MRR?"})
    assert ask.status_code == 503 and key_name in ask.json()["detail"]

    # Edited SQL still runs without an LLM: no summary, heuristic chart.
    run = client.post("/run", json={"sql": "SELECT name, monthly_price FROM plans"}).json()
    assert run["status"] == "ok" and run["summary"] is None and run["chart"]


def test_validation_errors(client):
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert client.post("/feedback", json={"trace_id": "x", "rating": 5}).status_code == 422
