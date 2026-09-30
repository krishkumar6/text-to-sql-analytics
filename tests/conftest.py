from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.llm import AnthropicLLM
from app.pipeline import Pipeline
from app.schema import SchemaCatalog
from app.traces import TraceStore


# ---- scripted stand-in for anthropic.Anthropic -----------------------------------------------------

def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text_response(payload: dict[str, Any], stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason, usage=_usage(),
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def tool_use_response(tool_input: dict[str, Any], tool_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="tool_use", usage=_usage(),
        content=[SimpleNamespace(type="tool_use", id=tool_id, name="lookup_column_values", input=tool_input)],
    )


def draft(sql: str = "", *, clarify: str = "", confidence: str = "high") -> SimpleNamespace:
    return text_response({
        "sql": sql, "explanation": "test explanation", "confidence": confidence,
        "needs_clarification": bool(clarify), "clarifying_question": clarify, "assumptions": [],
    })


def summary(text: str = "Summary.", kind: str = "none", x: str = "", y: list[str] | None = None) -> SimpleNamespace:
    return text_response({"summary": text, "chart": {"kind": kind, "x": x, "y": y or [], "series": "", "title": ""}})


class FakeMessages:
    def __init__(self) -> None:
        self.script: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        # Snapshot the message list: the caller keeps appending to the same object.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            raise AssertionError("FakeClient received more requests than were scripted")
        return self.script.pop(0)


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()
        self.beta = SimpleNamespace(messages=self.messages)

    def queue(self, *responses: Any) -> FakeClient:
        self.messages.script.extend(responses)
        return self


# ---- database-backed fixtures (skipped when the Postgres container isn't running) ------------------

@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def db(settings: Settings) -> Database:
    database = Database(settings.database_url, statement_timeout_ms=settings.statement_timeout_ms,
                        max_rows=settings.max_rows)
    if not database.ping():
        pytest.skip("Postgres is not reachable; run `docker compose up -d db` and `python -m scripts.seed`")
    return database


@pytest.fixture(scope="session")
def catalog(db: Database, settings: Settings) -> SchemaCatalog:
    return SchemaCatalog.load(db, settings.schemas, settings.sample_rows_per_table)


@pytest.fixture(scope="session")
def traces(settings: Settings) -> TraceStore:
    return TraceStore(settings.trace_database_url)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture(scope="session")
def business_context(settings: Settings) -> str:
    return settings.business_context_path.read_text(encoding="utf-8")


@pytest.fixture
def pipeline(settings, db, catalog, traces, fake_client, business_context) -> Pipeline:
    llm = AnthropicLLM(fake_client, settings, catalog, db, business_context)
    return Pipeline(settings, db, catalog, llm, traces)
