"""Step 6: persist one trace per question, plus user feedback.

Writes go through the app_logger role, which can only touch ops.query_traces. Tracing is
best-effort: a logging failure is reported but never fails the user's request.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)

TRACE_COLUMNS = (
    "trace_id", "question", "status", "sql", "explanation", "confidence", "summary", "error", "row_count",
    "attempts", "latency_ms", "step_latency_ms", "model", "input_tokens", "output_tokens", "cache_read_tokens",
)


class TraceStore:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=2) if url else None

    def record(self, trace: dict[str, Any]) -> None:
        if self.engine is None:
            return
        params = {k: trace.get(k) for k in TRACE_COLUMNS}
        params["step_latency_ms"] = json.dumps(params["step_latency_ms"] or {})
        placeholders = ", ".join(
            "CAST(:step_latency_ms AS jsonb)" if c == "step_latency_ms" else f":{c}" for c in TRACE_COLUMNS
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(f"INSERT INTO ops.query_traces ({', '.join(TRACE_COLUMNS)}) VALUES ({placeholders})"),
                    params,
                )
        except SQLAlchemyError:
            log.exception("Failed to record trace %s", trace.get("trace_id"))

    def add_feedback(self, trace_id: uuid.UUID, rating: int, comment: str | None) -> bool:
        if self.engine is None:
            return False
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "UPDATE ops.query_traces SET feedback = :rating, feedback_comment = :comment, "
                    "feedback_at = now() WHERE trace_id = :trace_id RETURNING trace_id"
                ),
                {"rating": rating, "comment": comment, "trace_id": trace_id},
            ).first()
        return row is not None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if self.engine is None:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM ops.query_traces ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}
            ).mappings().all()
        return [dict(r) for r in rows]
