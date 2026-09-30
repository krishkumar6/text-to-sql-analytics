-- Runs once, when the Postgres volume is first initialised.
-- Dev-only passwords; use secrets in any shared environment.

-- analytics_ro: the role LLM-generated SQL runs as. This is the real security boundary:
-- SELECT-only grants, read-only transactions by default, and a hard statement timeout.
CREATE ROLE analytics_ro LOGIN PASSWORD 'analytics_ro';
ALTER ROLE analytics_ro SET default_transaction_read_only = on;
ALTER ROLE analytics_ro SET statement_timeout = '15s';
ALTER ROLE analytics_ro SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE analytics TO analytics_ro;
GRANT USAGE ON SCHEMA public TO analytics_ro;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Tables the seed script creates later (as postgres) become readable automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO analytics_ro;

-- Trace log. Lives in its own schema that analytics_ro cannot see, written by a role
-- that can touch nothing else.
CREATE SCHEMA ops;

CREATE TABLE ops.query_traces (
    trace_id         uuid PRIMARY KEY,
    created_at       timestamptz NOT NULL DEFAULT now(),
    question         text        NOT NULL,
    status           text        NOT NULL,  -- ok | clarification | rejected | error
    sql              text,
    explanation      text,
    confidence       text,
    summary          text,
    error            text,
    row_count        integer,
    attempts         integer     NOT NULL DEFAULT 1,
    latency_ms       integer     NOT NULL,
    step_latency_ms  jsonb       NOT NULL DEFAULT '{}',
    model            text,
    input_tokens     integer     NOT NULL DEFAULT 0,
    output_tokens    integer     NOT NULL DEFAULT 0,
    cache_read_tokens integer    NOT NULL DEFAULT 0,
    feedback         smallint    CHECK (feedback IN (-1, 1)),
    feedback_comment text,
    feedback_at      timestamptz
);
CREATE INDEX query_traces_created_at_idx ON ops.query_traces (created_at DESC);

CREATE ROLE app_logger LOGIN PASSWORD 'app_logger';
GRANT CONNECT ON DATABASE analytics TO app_logger;
GRANT USAGE ON SCHEMA ops TO app_logger;
GRANT SELECT, INSERT, UPDATE ON ops.query_traces TO app_logger;
