"""LLM calls: drafting SQL (with a value-lookup tool) and summarising results.

Both calls return typed objects validated by pydantic, never free text that has to be parsed.
Two providers are supported behind one interface:

- AnthropicLLM: structured output (output_config.format) alongside the lookup tool.
- GroqLLM: Groq rejects JSON-schema output combined with tools, so the draft is returned through a
  strict `submit_sql_draft` tool call (tool_choice="required"); the summary uses JSON-schema output.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.db import Database, QueryError, QueryResult
from app.schema import SchemaCatalog

MAX_TOKENS = 16_000


# ---- errors (provider-neutral; the API layer maps these to HTTP codes) ----------------------------

class LLMError(Exception):
    """The model call finished without a usable answer (refusal, truncation, invalid output)."""


class LLMUnavailable(LLMError):
    """Credentials are missing or invalid."""


class LLMRateLimited(LLMError):
    """The provider is rate-limiting us. `retry_after` is in seconds when the provider said."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(exc: Any) -> float | None:
    value = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class LLMUpstreamError(LLMError):
    """Network failure or provider-side error."""


# ---- typed outputs -------------------------------------------------------------------------------

class SQLDraft(BaseModel):
    sql: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    needs_clarification: bool
    clarifying_question: str
    assumptions: list[str]


class ChartSpec(BaseModel):
    kind: Literal["none", "bar", "line", "scatter", "pie"]
    x: str
    y: list[str]
    series: str
    title: str


class ResultSummary(BaseModel):
    summary: str
    chart: ChartSpec


SQL_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "description": "One PostgreSQL SELECT statement, or \"\" when clarification is needed."},
        "explanation": {"type": "string", "description": "One or two sentences on how the query answers the question."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "needs_clarification": {"type": "boolean"},
        "clarifying_question": {"type": "string", "description": "The question to ask the user, or \"\"."},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sql", "explanation", "confidence", "needs_clarification", "clarifying_question", "assumptions"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "chart": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["none", "bar", "line", "scatter", "pie"]},
                "x": {"type": "string", "description": "Column for the x axis (pie: slice labels), or \"\"."},
                "y": {"type": "array", "items": {"type": "string"}, "description": "Numeric column(s) to plot."},
                "series": {"type": "string", "description": "Optional column to split into series/colors, or \"\"."},
                "title": {"type": "string"},
            },
            "required": ["kind", "x", "y", "series", "title"],
            "additionalProperties": False,
        },
    },
    "required": ["summary", "chart"],
    "additionalProperties": False,
}

LOOKUP_TOOL_NAME = "lookup_column_values"
LOOKUP_TOOL_DESCRIPTION = (
    "Return the most common stored values of one column (up to 20, with row counts), optionally "
    "filtered by a case-insensitive substring. Use it before filtering on a text value you have not "
    "seen in the schema, e.g. a company name or an unusual spelling."
)
LOOKUP_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "table": {"type": "string", "description": "Table name from the schema."},
        "column": {"type": "string", "description": "Column name in that table."},
        "search": {"type": "string", "description": "Substring to match, or \"\" for the top values."},
    },
    "required": ["table", "column", "search"],
    "additionalProperties": False,
}

SUBMIT_TOOL_NAME = "submit_sql_draft"
SUBMIT_TOOL_DESCRIPTION = "Submit your final draft for the user's question. Call it once, when you are done."


# ---- prompts -------------------------------------------------------------------------------------

SQL_SYSTEM_PROMPT = """\
You translate business questions into a single PostgreSQL query for an analytics database. \
The query runs as a read-only role, its result is shown to the user as a table and chart, and a \
separate step writes the prose summary, so the query result itself must answer the question.

<business_context>
{business_context}
</business_context>

<schema>
{schema}
</schema>

Today's date is {today}.

Writing the query:
- Use only the tables and columns in the schema, and the metric definitions in the business context.
- Filter on text values exactly as stored. The schema lists the values of low-cardinality columns; for any \
other text filter, check real values with lookup_column_values first rather than guessing spelling or case.
- Return presentation-ready results: aggregate in SQL, alias columns in readable snake_case, round money to \
2 decimals and percentages to 1, order rows meaningfully, and use LIMIT for top-N questions. Time series \
should have one row per period with the period column first.
- One statement, SELECT or WITH only.

When the question is ambiguous, prefer answering over asking: pick the most common business interpretation, \
answer it, and list the interpretation in assumptions. Set needs_clarification only when reasonable \
interpretations would produce materially different answers and nothing in the context favours one, or when \
the data cannot answer the question at all; then leave sql empty and put a short question to the user in \
clarifying_question.

confidence reflects whether the query answers the question as asked: high when the schema and definitions \
map directly, medium when you had to make assumptions, low when the mapping is loose.

If a message tells you a query failed, read the database error, fix the cause, and return a corrected draft.\
{output_instruction}"""

SUMMARY_SYSTEM_PROMPT = """\
You describe the result of an analytics query in one sentence and choose how to chart it.

summary: one plain sentence (at most ~30 words) that answers the user's question with the key numbers from \
the result, e.g. the total, the top item, or the trend from first to last period. Use only numbers present \
in the result. If only some rows are shown or the result is empty, say so rather than extrapolating.

chart: choose from the result's columns.
- line: a time or ordered period on x with numeric measures on y.
- bar: categories on x (roughly 2-30 of them) with numeric measures on y.
- scatter: two numeric measures, one on x and one on y.
- pie: share of a total across 2-6 categories only.
- none: a single row or value, a wide lookup table, or nothing sensible to plot.
Use series for a category column that splits a line or bar chart into groups (long-format data); otherwise "". \
x, y and series must be exact column names from the result.\
"""


# ---- shared state --------------------------------------------------------------------------------

@dataclass
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_calls: int = 0

    def add(self, *, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write


@dataclass
class DraftSession:
    """Provider-format message history for one question, so a failed query is repaired in context."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    lookups: int = 0


class LLM(ABC):
    provider: str

    def __init__(
        self, client: Any, settings: Settings, catalog: SchemaCatalog, db: Database, business_context: str
    ) -> None:
        self.client = client
        self.settings = settings
        self.catalog = catalog
        self.db = db
        self.sql_system = SQL_SYSTEM_PROMPT.format(
            business_context=business_context.strip(),
            schema=catalog.render(),
            today=date.today().isoformat(),
            output_instruction=self.output_instruction,
        )

    output_instruction = ""

    # -- public API used by the pipeline ---------------------------------------------------------

    def draft_sql(self, question: str, usage: Usage) -> tuple[SQLDraft, DraftSession]:
        session = self._new_session(question)
        return self._run_draft(session, usage), session

    def repair_sql(self, session: DraftSession, failed_sql: str, error: str, usage: Usage) -> SQLDraft:
        session.messages.append({
            "role": "user",
            "content": (
                f"The query was rejected before returning results.\n\n<sql>\n{failed_sql}\n</sql>\n\n"
                f"<error>\n{error}\n</error>\n\nReturn a corrected draft."
            ),
        })
        return self._run_draft(session, usage)

    def summarize(self, question: str, result: QueryResult, usage: Usage, sample_rows: int = 40) -> ResultSummary:
        shown = result.rows[:sample_rows]
        note = f"{result.row_count} rows returned"
        if result.truncated:
            note += " (the query hit the row cap, so there are more)"
        if len(shown) < result.row_count:
            note += f"; the first {len(shown)} are shown"
        content = (
            f"<question>{question}</question>\n<sql>\n{result.sql}\n</sql>\n"
            f"<result>\n{note}.\ncolumns: {json.dumps(result.columns)}\n"
            + "\n".join(json.dumps(row, ensure_ascii=False) for row in shown)
            + "\n</result>"
        )
        return self._structured(SUMMARY_SYSTEM_PROMPT, content, SUMMARY_SCHEMA, ResultSummary,
                                self.settings.summary_effort, usage)

    # -- provider hooks --------------------------------------------------------------------------

    @abstractmethod
    def _new_session(self, question: str) -> DraftSession: ...

    @abstractmethod
    def _run_draft(self, session: DraftSession, usage: Usage) -> SQLDraft: ...

    @abstractmethod
    def _structured(self, system: str, content: str, schema: dict, model: type[BaseModel], effort: str,
                    usage: Usage) -> Any: ...

    # -- the lookup tool (shared) ----------------------------------------------------------------

    @property
    def _max_turns(self) -> int:
        # Each lookup round is one request; the extra 2 cover the final answer and one malformed turn.
        return self.settings.max_lookup_calls + 2

    def _call_lookup(self, session: DraftSession, args: dict[str, Any]) -> tuple[str, bool]:
        if session.lookups >= self.settings.max_lookup_calls:
            return "Lookup limit reached. Write the query with what you know now.", True
        session.lookups += 1
        try:
            return self._lookup_column_values(str(args["table"]), str(args["column"]), str(args.get("search", "")))
        except KeyError as exc:
            return f"Missing argument {exc}.", True

    def _lookup_column_values(self, table: str, column: str, search: str) -> tuple[str, bool]:
        info = self.catalog.get(table)
        if info is None:
            return f"Unknown table {table!r}.", True
        if info.column(column) is None:
            return f"Table {info.qualified} has no column {column!r}.", True
        q = self.db.quote_identifier
        # Identifiers are checked against the catalog above; the search term is a bound parameter.
        sql = (
            f"SELECT {q(column)}::text AS value, count(*) AS row_count "
            f"FROM {q(info.schema)}.{q(info.name)} "
            f"WHERE {q(column)} IS NOT NULL AND (:search = '' OR {q(column)}::text ILIKE :pattern) "
            f"GROUP BY 1 ORDER BY row_count DESC LIMIT 20"
        )
        try:
            result = self.db.execute(sql, {"search": search, "pattern": f"%{search}%"})
        except QueryError as exc:
            return f"Lookup failed: {exc}", True
        if not result.rows:
            return "No matching values.", False
        return json.dumps([{"value": v, "rows": n} for v, n in result.rows]), False


def _validate(model: type[BaseModel], raw: str) -> Any:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise LLMError(f"The model returned output that doesn't match the schema: {exc.errors()[:2]}") from exc


# ---- Anthropic -----------------------------------------------------------------------------------

class AnthropicLLM(LLM):
    provider = "anthropic"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    LOOKUP_TOOL = {"name": LOOKUP_TOOL_NAME, "description": LOOKUP_TOOL_DESCRIPTION, "strict": True,
                   "input_schema": LOOKUP_TOOL_SCHEMA}

    def _new_session(self, question: str) -> DraftSession:
        return DraftSession(messages=[{"role": "user", "content": question}])

    def _run_draft(self, session: DraftSession, usage: Usage) -> SQLDraft:
        for _ in range(self._max_turns):
            response = self._create(self.sql_system, session.messages, [self.LOOKUP_TOOL], SQL_DRAFT_SCHEMA,
                                    self.settings.sql_effort, usage)
            session.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = self._call_lookup(session, block.input) if block.name == LOOKUP_TOOL_NAME \
                        else (f"Unknown tool {block.name}.", True)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output,
                                    "is_error": is_error})
                session.messages.append({"role": "user", "content": results})
                continue
            return _validate(SQLDraft, self._final_text(response))
        raise LLMError("The model kept looking up values without producing a query.")

    def _structured(self, system, content, schema, model, effort, usage):
        response = self._create(system, [{"role": "user", "content": content}], None, schema, effort, usage)
        return _validate(model, self._final_text(response))

    def _create(self, system: str, messages, tools, schema, effort: str, usage: Usage):
        import anthropic

        kwargs: dict[str, Any] = {"tools": tools} if tools else {}
        with self._translate_errors(anthropic):
            response = self.client.beta.messages.create(
                model=self.settings.model,
                max_tokens=MAX_TOKENS,
                # The system prompt (schema + business context) is identical across requests: cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines, the API re-runs the request on a fallback model.
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
                **kwargs,
            )
        u = response.usage
        usage.add(input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                  cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                  cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)
        return response

    @staticmethod
    def _final_text(response: Any) -> str:
        if response.stop_reason == "refusal":
            raise LLMError("The model declined to answer this question.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off before it finished.")
        text = next((b.text for b in reversed(response.content) if b.type == "text"), None)
        if text is None:
            raise LLMError(f"The model returned no answer (stop_reason={response.stop_reason}).")
        return text

    @staticmethod
    @contextmanager
    def _translate_errors(anthropic):
        try:
            yield
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"Anthropic rejected the credentials: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimited("Anthropic is rate-limiting requests. Try again shortly.",
                                 _retry_after(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise LLMUpstreamError(f"Anthropic request failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUpstreamError(f"Could not reach Anthropic: {exc}") from exc
        except TypeError as exc:
            # The SDK raises a bare TypeError when no API key / auth token is configured.
            if "authentication method" in str(exc):
                raise LLMUnavailable("No Anthropic credentials configured. Set ANTHROPIC_API_KEY.") from exc
            raise


# ---- Groq ----------------------------------------------------------------------------------------

class GroqLLM(LLM):
    provider = "groq"
    output_instruction = (
        "\n\nWork through tool calls only: use lookup_column_values as needed, then call submit_sql_draft "
        "exactly once with your draft."
    )
    TOOLS = [
        {"type": "function", "function": {"name": LOOKUP_TOOL_NAME, "description": LOOKUP_TOOL_DESCRIPTION,
                                          "parameters": LOOKUP_TOOL_SCHEMA, "strict": True}},
        {"type": "function", "function": {"name": SUBMIT_TOOL_NAME, "description": SUBMIT_TOOL_DESCRIPTION,
                                          "parameters": SQL_DRAFT_SCHEMA, "strict": True}},
    ]

    def _new_session(self, question: str) -> DraftSession:
        return DraftSession(messages=[{"role": "system", "content": self.sql_system},
                                      {"role": "user", "content": question}])

    def _run_draft(self, session: DraftSession, usage: Usage) -> SQLDraft:
        for _ in range(self._max_turns):
            response = self._create(session.messages, usage, self.settings.sql_effort,
                                    tools=self.TOOLS, tool_choice="required")
            if response is None:  # the model produced a malformed tool call; try the turn again
                continue
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise LLMError("The model's answer was cut off before it finished.")
            calls = choice.message.tool_calls or []
            if not calls:
                session.messages.append({"role": "user", "content": "Call submit_sql_draft with your draft."})
                continue
            session.messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name, "arguments": c.function.arguments}}
                               for c in calls],
            })
            draft, results = None, []
            for call in calls:
                output, is_error, parsed = self._handle_call(session, call)
                draft = draft or parsed
                results.append({"role": "tool", "tool_call_id": call.id,
                                "content": f"Error: {output}" if is_error else output})
            # Every tool call must be answered before the next user message (e.g. a repair request).
            session.messages.extend(results)
            if draft is not None:
                return draft
        raise LLMError("The model did not submit a draft within the turn limit.")

    def _handle_call(self, session: DraftSession, call: Any) -> tuple[str, bool, SQLDraft | None]:
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            return "Arguments were not valid JSON.", True, None
        if call.function.name == SUBMIT_TOOL_NAME:
            try:
                return "Draft received.", False, SQLDraft.model_validate(args)
            except ValidationError as exc:
                return f"Draft did not match the schema: {exc.errors()[:2]}", True, None
        if call.function.name == LOOKUP_TOOL_NAME:
            output, is_error = self._call_lookup(session, args)
            return output, is_error, None
        return f"Unknown tool {call.function.name}.", True, None

    def _structured(self, system, content, schema, model, effort, usage):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        response_format = {"type": "json_schema", "json_schema": {"name": model.__name__, "schema": schema,
                                                                   "strict": True}}
        response = self._create(messages, usage, effort, response_format=response_format)
        if response is None:
            raise LLMError("The model returned malformed output.")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("The model's answer was cut off before it finished.")
        return _validate(model, choice.message.content or "")

    def _create(self, messages, usage: Usage, effort: str, **kwargs):
        """Returns None when Groq rejects a malformed tool call from the model (retryable)."""
        import groq

        if self.client is None:
            raise LLMUnavailable("No Groq credentials configured. Set GROQ_API_KEY.")
        if self.settings.model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = effort
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model, messages=messages, max_completion_tokens=MAX_TOKENS, **kwargs
            )
        except groq.AuthenticationError as exc:
            raise LLMUnavailable("Groq rejected the API key. Check GROQ_API_KEY.") from exc
        except groq.RateLimitError as exc:
            quota = "daily token quota" if "tokens per day" in str(exc) else "rate limit"
            retry_after = _retry_after(exc)
            when = f" Try again in about {max(1, round(retry_after / 60))} min." if retry_after else \
                " Try again shortly."
            raise LLMRateLimited(f"Groq {quota} reached for {self.settings.model}.{when}", retry_after) from exc
        except groq.BadRequestError as exc:
            if "tool_use_failed" in str(exc) or "output_parse_failed" in str(exc):
                usage.add(input_tokens=0, output_tokens=0)
                return None
            raise LLMUpstreamError(f"Groq request failed (400): {exc.message}") from exc
        except groq.APIStatusError as exc:
            raise LLMUpstreamError(f"Groq request failed ({exc.status_code}): {exc.message}") from exc
        except groq.APIConnectionError as exc:
            raise LLMUpstreamError(f"Could not reach Groq: {exc}") from exc
        u = response.usage
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage.add(input_tokens=(u.prompt_tokens or 0) - cached, output_tokens=u.completion_tokens or 0,
                  cache_read=cached)
        return response


def create_llm(settings: Settings, catalog: SchemaCatalog, db: Database, business_context: str,
               client: Any = None) -> LLM:
    if settings.provider == "groq":
        if client is None:
            import groq

            try:
                client = groq.Groq(max_retries=3)
            except groq.GroqError:  # no API key: the app still starts; /ask reports 503
                client = None
        return GroqLLM(client, settings, catalog, db, business_context)
    if settings.provider == "anthropic":
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=3)
        return AnthropicLLM(client, settings, catalog, db, business_context)
    raise ValueError(f"Unknown LLM_PROVIDER {settings.provider!r}; use 'groq' or 'anthropic'.")
