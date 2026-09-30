from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from plotly.offline import get_plotlyjs
from pydantic import BaseModel, Field

from app.config import ROOT, Settings
from app.db import Database
from app.llm import LLMError, LLMRateLimited, LLMUnavailable, create_llm
from app.pipeline import Outcome, Pipeline
from app.schema import SchemaCatalog
from app.traces import TraceStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC_DIR = ROOT / "app" / "static"


def build_pipeline(settings: Settings) -> Pipeline:
    db = Database(settings.database_url, statement_timeout_ms=settings.statement_timeout_ms, max_rows=settings.max_rows)
    catalog = SchemaCatalog.load(db, settings.schemas, settings.sample_rows_per_table)
    business_context = settings.business_context_path.read_text(encoding="utf-8")
    llm = create_llm(settings, catalog, db, business_context)
    return Pipeline(settings, db, catalog, llm, TraceStore(settings.trace_database_url))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    app.state.pipeline = build_pipeline(settings)
    log.info("Loaded %d tables; provider=%s model=%s", len(app.state.pipeline.catalog.tables),
             settings.provider, settings.model)
    yield
    app.state.pipeline.db.engine.dispose()


app = FastAPI(title="Text-to-SQL Analytics", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- API models ----------------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class RunRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    question: str = Field(default="", max_length=2000)


class FeedbackRequest(BaseModel):
    trace_id: uuid.UUID
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2000)


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    llm_calls: int


class AskResponse(BaseModel):
    trace_id: uuid.UUID
    status: Literal["ok", "clarification", "rejected", "error"]
    question: str
    sql: str | None
    explanation: str | None
    confidence: str | None
    assumptions: list[str]
    clarifying_question: str | None
    summary: str | None
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    chart: dict[str, Any] | None
    error: str | None
    attempts: int
    latency_ms: int
    step_latency_ms: dict[str, int]
    usage: TokenUsage

    @classmethod
    def from_outcome(cls, o: Outcome) -> AskResponse:
        return cls(
            trace_id=o.trace_id, status=o.status, question=o.question, sql=o.sql, explanation=o.explanation,
            confidence=o.confidence, assumptions=o.assumptions, clarifying_question=o.clarifying_question,
            summary=o.summary, columns=o.columns, rows=o.rows, row_count=o.row_count, truncated=o.truncated,
            chart=o.chart, error=o.error, attempts=o.attempts, latency_ms=o.latency_ms,
            step_latency_ms=o.step_latency_ms,
            usage=TokenUsage(
                input_tokens=o.usage.input_tokens + o.usage.cache_read_tokens + o.usage.cache_write_tokens,
                output_tokens=o.usage.output_tokens,
                cache_read_tokens=o.usage.cache_read_tokens,
                llm_calls=o.usage.llm_calls,
            ),
        )


# ---- endpoints -----------------------------------------------------------------------------------

def _pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> AskResponse:
    try:
        outcome = _pipeline(request).ask(body.question.strip())
    except LLMUnavailable as exc:
        raise HTTPException(503, str(exc))
    except LLMRateLimited as exc:
        raise HTTPException(429, str(exc))
    except LLMError as exc:  # upstream failures, refusals, invalid output
        raise HTTPException(502, str(exc))
    return AskResponse.from_outcome(outcome)


@app.post("/run", response_model=AskResponse)
def run_sql(body: RunRequest, request: Request) -> AskResponse:
    """Execute SQL edited by the user, through the same guard and read-only role."""
    return AskResponse.from_outcome(_pipeline(request).run_sql(body.sql, body.question.strip()))


@app.get("/tables")
def tables(request: Request) -> list[dict[str, Any]]:
    return _pipeline(request).catalog.to_dict()


@app.post("/feedback")
def feedback(body: FeedbackRequest, request: Request) -> dict[str, bool]:
    if not _pipeline(request).traces.add_feedback(body.trace_id, body.rating, body.comment):
        raise HTTPException(404, "Unknown trace_id (or tracing is disabled).")
    return {"ok": True}


@app.get("/traces")
def traces(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return _pipeline(request).traces.recent(min(max(limit, 1), 500))


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    pipeline = _pipeline(request)
    return {"database": pipeline.db.ping(), "provider": pipeline.settings.provider, "model": pipeline.settings.model}


@lru_cache(maxsize=1)
def _plotly_js() -> str:
    return get_plotlyjs()


@app.get("/plotly.min.js", include_in_schema=False)
def plotly_js() -> Response:
    # Served from the installed plotly package so the JS always matches the figure JSON we generate.
    return Response(_plotly_js(), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
