from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "anthropic": "claude-opus-5-5"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # Connects as analytics_ro: LLM SQL runs with SELECT-only grants.
    database_url: str
    # Connects as app_logger: can only write ops.query_traces. Empty = tracing off.
    trace_database_url: str
    provider: str  # "groq" or "anthropic"
    model: str
    sql_effort: str
    summary_effort: str
    statement_timeout_ms: int
    max_rows: int
    rows_in_response: int
    sample_rows_per_table: int
    max_lookup_calls: int
    schemas: tuple[str, ...]
    business_context_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        provider = os.getenv("LLM_PROVIDER") or (
            "groq" if os.getenv("GROQ_API_KEY") and not os.getenv("ANTHROPIC_API_KEY") else "anthropic"
        )
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql+psycopg://analytics_ro:analytics_ro@localhost:5434/analytics"
            ),
            trace_database_url=os.getenv(
                "TRACE_DATABASE_URL", "postgresql+psycopg://app_logger:app_logger@localhost:5434/analytics"
            ),
            provider=provider.lower(),
            model=os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(provider.lower(), ""),
            sql_effort=os.getenv("LLM_SQL_EFFORT", "medium"),
            summary_effort=os.getenv("LLM_SUMMARY_EFFORT", "low"),
            statement_timeout_ms=_int("STATEMENT_TIMEOUT_MS", 10_000),
            max_rows=_int("MAX_ROWS", 1_000),
            rows_in_response=_int("ROWS_IN_RESPONSE", 500),
            sample_rows_per_table=_int("SAMPLE_ROWS_PER_TABLE", 3),
            max_lookup_calls=_int("MAX_LOOKUP_CALLS", 4),
            schemas=tuple(s.strip() for s in os.getenv("DB_SCHEMAS", "public").split(",") if s.strip()),
            business_context_path=Path(os.getenv("BUSINESS_CONTEXT_PATH", ROOT / "db" / "business_context.md")),
        )
