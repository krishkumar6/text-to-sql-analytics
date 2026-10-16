from __future__ import annotations

import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from plotly.offline import get_plotlyjs
from pydantic import BaseModel, Field

from app.config import ROOT, Settings
from app.db import Database
from app.llm import LLMError, LLMRateLimited, LLMUnavailable, create_llm
from app.pipeline import Outcome, Pipeline
from app.ratelimit import RateLimiter
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
    app.state.limiter = RateLimiter(settings.rate_limit_per_client, settings.rate_limit_window_s,
                                    settings.daily_question_cap)
    log.info("Loaded %d tables; provider=%s model=%s", len(app.state.pipeline.catalog.tables),
             settings.provider, settings.model)
    yield
    app.state.pipeline.db.engine.dispose()


API_DESCRIPTION = """
Ask a business question in plain English and get back the SQL that answers it, the result rows,
a chart and a one-line summary.

**How a question is answered**

1. The LLM drafts SQL as structured output (SQL, explanation, confidence, or a clarifying question),
   using the schema and business definitions such as MRR and churn.
2. The SQL is validated (single read-only `SELECT`, allowlisted tables, no dangerous functions, `LIMIT` added).
3. It runs as a SELECT-only database role, in a read-only transaction with a timeout and row cap.
   If it fails, the database error goes back to the LLM for one repair attempt.
4. The result is summarised in one sentence and charted.

The demo database is a fictional SaaS company with 12 months of data. This public demo is
rate-limited per visitor and per day because it runs on a free LLM tier.

[Source code and evaluation results on GitHub](https://github.com/krishkumar6/text-to-sql-analytics)
"""

OPENAPI_TAGS = [
    {"name": "Query", "description": "Ask questions in plain English, or run SQL directly. Both go through the same safety checks."},
    {"name": "Schema", "description": "The tables and columns the model can query."},
    {"name": "Feedback", "description": "Rate an answer. Thumbs-down answers become new evaluation cases."},
    {"name": "Health", "description": "Service and database status."},
]

LLM_ERRORS: dict[int | str, dict[str, Any]] = {
    429: {"description": "Rate limit reached (per visitor or daily cap), or the LLM provider's quota is exhausted."},
    502: {"description": "The LLM provider failed, or returned output that didn't match the schema."},
    503: {"description": "No LLM credentials are configured on the server."},
}

app = FastAPI(
    title="Text-to-SQL Analytics",
    version="1.0.0",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- API models ----------------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000, description="A business question in plain English.",
                          examples=["What is current MRR by plan?"])


class RunRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000,
                     description="A PostgreSQL SELECT query. Anything else is rejected by the SQL guard.",
                     examples=["SELECT name, monthly_price FROM plans ORDER BY tier_rank"])
    question: str = Field(default="", max_length=2000,
                          description="Optional: the question the SQL answers, used for the summary.",
                          examples=["List the plans and their prices"])


class FeedbackRequest(BaseModel):
    trace_id: uuid.UUID = Field(description="The `trace_id` returned by /ask or /run.")
    rating: Literal[1, -1] = Field(description="1 = correct, -1 = wrong.", examples=[1])
    comment: str | None = Field(default=None, max_length=2000, description="Optional note on what was wrong.")


class TokenUsage(BaseModel):
    input_tokens: int = Field(description="Tokens sent to the LLM, summed over all calls for this question.")
    output_tokens: int = Field(description="Tokens the LLM generated.")
    cache_read_tokens: int = Field(description="Input tokens served from the provider's prompt cache.")
    llm_calls: int = Field(description="Number of LLM calls (usually 2: draft SQL, then summarise).")


class AskResponse(BaseModel):
    trace_id: uuid.UUID = Field(description="ID of this answer; send it to /feedback.")
    status: Literal["ok", "clarification", "rejected", "error"] = Field(
        description="`ok` = answered; `clarification` = the model asks a question instead of guessing; "
                    "`rejected` = the SQL guard blocked the query; `error` = the database rejected it.")
    question: str
    sql: str | None = Field(description="The validated SQL that was executed (LIMIT added if missing).")
    explanation: str | None = Field(description="How the query answers the question.")
    confidence: str | None = Field(description="The model's self-reported confidence: high, medium or low.")
    assumptions: list[str] = Field(description="Interpretations the model made for an ambiguous question.")
    clarifying_question: str | None = Field(description="Set when status is `clarification`.")
    summary: str | None = Field(description="One-sentence answer based on the result rows.")
    columns: list[str] = Field(description="Result column names.")
    rows: list[list[Any]] = Field(description="Result rows (capped for the response).")
    row_count: int
    truncated: bool = Field(description="True if the query hit the row cap.")
    chart: dict[str, Any] | None = Field(description="A Plotly figure (data + layout), or null if no chart fits.")
    error: str | None = Field(description="Guard or database error message, when status is rejected/error.")
    attempts: int = Field(description="1, or 2 if the first query failed and the LLM repaired it.")
    latency_ms: int = Field(description="Total time to answer.")
    step_latency_ms: dict[str, int] = Field(description="Time per pipeline step (generate, validate, execute, …).")
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


def enforce_limits(request: Request) -> None:
    """Per-client and daily limits on endpoints that spend LLM tokens."""
    # Behind a proxy, uvicorn's --proxy-headers makes this the real client IP (see Dockerfile).
    client = request.client.host if request.client else "unknown"
    refusal = request.app.state.limiter.check(client)
    if refusal:
        raise HTTPException(429, refusal)


def require_admin(request: Request) -> None:
    token = _pipeline(request).settings.admin_token
    if token and not hmac.compare_digest(request.headers.get("x-admin-token", ""), token):
        raise HTTPException(404, "Not Found")  # don't advertise that the endpoint exists


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(enforce_limits)], tags=["Query"],
          summary="Ask a question in plain English", responses=LLM_ERRORS)
def ask(body: AskRequest, request: Request) -> AskResponse:
    """Turn a natural-language question into SQL, run it read-only, and return the rows, a chart and a
    one-line summary. If the question can't be answered from the data, `status` is `clarification`."""
    try:
        outcome = _pipeline(request).ask(body.question.strip())
    except LLMUnavailable as exc:
        raise HTTPException(503, str(exc))
    except LLMRateLimited as exc:
        raise HTTPException(429, str(exc))
    except LLMError as exc:  # upstream failures, refusals, invalid output
        raise HTTPException(502, str(exc))
    return AskResponse.from_outcome(outcome)


@app.post("/run", response_model=AskResponse, dependencies=[Depends(enforce_limits)], tags=["Query"],
          summary="Run your own SQL", responses={429: LLM_ERRORS[429]})
def run_sql(body: RunRequest, request: Request) -> AskResponse:
    """Execute SQL you wrote or edited, through the same guard and read-only database role as /ask.
    Try `DROP TABLE users`: it is rejected before reaching the database."""
    return AskResponse.from_outcome(_pipeline(request).run_sql(body.sql, body.question.strip()))


@app.get("/tables", tags=["Schema"], summary="List tables and columns")
def tables(request: Request) -> list[dict[str, Any]]:
    """Tables, columns, types, keys, descriptions and approximate row counts: the schema the LLM sees."""
    return _pipeline(request).catalog.to_dict()


@app.post("/feedback", tags=["Feedback"], summary="Rate an answer",
          responses={404: {"description": "Unknown trace_id."}})
def feedback(body: FeedbackRequest, request: Request) -> dict[str, bool]:
    """Record 👍 (1) or 👎 (-1) for an answer, identified by the `trace_id` from /ask or /run."""
    if not _pipeline(request).traces.add_feedback(body.trace_id, body.rating, body.comment):
        raise HTTPException(404, "Unknown trace_id (or tracing is disabled).")
    return {"ok": True}


# Internal admin route: protected by X-Admin-Token and left out of the public docs.
@app.get("/traces", dependencies=[Depends(require_admin)], include_in_schema=False)
def traces(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return _pipeline(request).traces.recent(min(max(limit, 1), 500))


@app.get("/health", tags=["Health"], summary="Service status")
def health(request: Request) -> dict[str, Any]:
    """Whether the database is reachable, and which LLM provider and model are in use."""
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
