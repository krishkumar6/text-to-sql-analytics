"""The question -> answer pipeline.

1. schema context (built once at startup, in the LLM's system prompt)
2. LLM drafts SQL        -> SQLDraft {sql, explanation, confidence, needs_clarification, ...}
3. validate              -> single SELECT, allowlisted tables, no forbidden constructs, LIMIT added
4. execute               -> analytics_ro role, READ ONLY transaction, statement_timeout, row cap
                            on a validation or database error: send it back to the LLM, retry once
5. format                -> one-line summary + chart (LLM), rows for the table
6. trace                 -> question, SQL, rows, latency per step, tokens; feedback added later
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from app.charts import build_figure
from app.config import Settings
from app.db import Database, QueryError, QueryResult
from app.guard import UnsafeSQLError, validate_sql
from app.llm import LLM, ChartSpec, LLMError, SQLDraft, Usage
from app.schema import SchemaCatalog
from app.traces import TraceStore

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # the first try plus one LLM repair

Status = Literal["ok", "clarification", "rejected", "error"]


class StepTimer:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.steps: dict[str, int] = {}

    @contextmanager
    def __call__(self, step: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.steps[step] = self.steps.get(step, 0) + int((time.perf_counter() - t0) * 1000)

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


@dataclass
class Outcome:
    question: str
    model: str
    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: Status = "error"
    sql: str | None = None
    explanation: str | None = None
    confidence: str | None = None
    assumptions: list[str] = field(default_factory=list)
    clarifying_question: str | None = None
    summary: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict[str, Any] | None = None
    chart_spec: ChartSpec | None = None
    error: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    step_latency_ms: dict[str, int] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)

    def apply_draft(self, draft: SQLDraft) -> None:
        self.sql = draft.sql or None
        self.explanation = draft.explanation
        self.confidence = draft.confidence
        self.assumptions = draft.assumptions
        self.clarifying_question = draft.clarifying_question or None

    def trace_row(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "status": self.status,
            "sql": self.sql,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "summary": self.summary,
            "error": self.error,
            "row_count": self.row_count if self.status == "ok" else None,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "step_latency_ms": self.step_latency_ms,
            "model": self.model,
            "input_tokens": self.usage.input_tokens + self.usage.cache_read_tokens + self.usage.cache_write_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
        }


class Pipeline:
    def __init__(
        self, settings: Settings, db: Database, catalog: SchemaCatalog, llm: LLM, traces: TraceStore
    ) -> None:
        self.settings = settings
        self.db = db
        self.catalog = catalog
        self.llm = llm
        self.traces = traces

    def ask(self, question: str) -> Outcome:
        """Answer a natural-language question. LLM/API exceptions propagate after the trace is written."""
        out = Outcome(question=question, model=self.settings.model)
        timer = StepTimer()
        try:
            with timer("generate"):
                draft, session = self.llm.draft_sql(question, out.usage)
            out.apply_draft(draft)

            for attempt in range(1, MAX_ATTEMPTS + 1):
                if draft.needs_clarification or not draft.sql.strip():
                    out.status = "clarification"
                    out.clarifying_question = draft.clarifying_question or "Could you rephrase the question?"
                    return out
                out.attempts = attempt
                try:
                    result = self._validate_and_execute(draft.sql, out, timer)
                    break
                except (UnsafeSQLError, QueryError) as exc:
                    out.status = "rejected" if isinstance(exc, UnsafeSQLError) else "error"
                    out.error = str(exc)
                    if attempt == MAX_ATTEMPTS:
                        return out
                    log.info("Attempt %d failed (%s); asking the LLM to repair it", attempt, out.error)
                    with timer("repair"):
                        draft = self.llm.repair_sql(session, draft.sql, out.error, out.usage)
                    out.apply_draft(draft)

            self._format(out, result, timer)
            return out
        except Exception as exc:
            out.status, out.error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            out.latency_ms, out.step_latency_ms = timer.total_ms, timer.steps
            self.traces.record(out.trace_row())

    def run_sql(self, sql: str, question: str = "") -> Outcome:
        """Run SQL the user edited by hand. Same guard and read-only execution; no LLM repair."""
        out = Outcome(question=question or "(edited SQL)", model=self.settings.model, attempts=1)
        timer = StepTimer()
        try:
            try:
                result = self._validate_and_execute(sql, out, timer)
            except (UnsafeSQLError, QueryError) as exc:
                out.sql = sql
                out.status = "rejected" if isinstance(exc, UnsafeSQLError) else "error"
                out.error = str(exc)
                return out
            self._format(out, result, timer)
            return out
        finally:
            out.latency_ms, out.step_latency_ms = timer.total_ms, timer.steps
            self.traces.record(out.trace_row())

    def _validate_and_execute(self, sql: str, out: Outcome, timer: StepTimer) -> QueryResult:
        with timer("validate"):
            safe_sql = validate_sql(sql, allowed_tables=self.catalog.allowed_tables, max_rows=self.settings.max_rows)
        out.sql = safe_sql
        with timer("execute"):
            return self.db.execute(safe_sql)

    def _format(self, out: Outcome, result: QueryResult, timer: StepTimer) -> None:
        out.status, out.error = "ok", None
        out.columns = result.columns
        out.rows = result.rows[: self.settings.rows_in_response]
        out.row_count = result.row_count
        out.truncated = result.truncated

        spec = None
        with timer("summarize"):
            try:
                summary = self.llm.summarize(out.question, result, out.usage)
                out.summary, spec = summary.summary, summary.chart
            except LLMError as exc:
                # The answer is still useful without prose; fall back to a heuristic chart.
                log.warning("Summary step failed: %s", exc)
        with timer("chart"):
            out.chart, out.chart_spec = build_figure(spec, result)
