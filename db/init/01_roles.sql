-- Roles and the trace table. Portable and idempotent:
--   * local Docker runs it automatically on first boot (as postgres);
--   * hosted Postgres (Neon, Supabase, RDS…) gets it from `python -m scripts.setup_db`, which runs it as
--     the database owner and then replaces the dev passwords below with real ones.
-- It never names the owner role or the database, so it works whatever the provider calls them.

-- analytics_ro: the role LLM-generated SQL runs as. This is the real security boundary:
-- SELECT-only grants, read-only transactions by default, and a hard statement timeout.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_ro') THEN
        CREATE ROLE analytics_ro LOGIN PASSWORD 'analytics_ro';  -- dev password; setup_db replaces it
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_logger') THEN
        CREATE ROLE app_logger LOGIN PASSWORD 'app_logger';      -- dev password; setup_db replaces it
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO analytics_ro, app_logger', current_database());
    -- Tables the owner creates later (the seed script) become readable automatically.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO analytics_ro',
                   current_user);
END
$$;

ALTER ROLE analytics_ro SET default_transaction_read_only = on;
ALTER ROLE analytics_ro SET statement_timeout = '15s';
ALTER ROLE analytics_ro SET idle_in_transaction_session_timeout = '30s';

GRANT USAGE ON SCHEMA public TO analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Trace log. Lives in its own schema that analytics_ro cannot see, written by a role that can touch
-- nothing else.
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.query_traces (
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
CREATE INDEX IF NOT EXISTS query_traces_created_at_idx ON ops.query_traces (created_at DESC);

REVOKE ALL ON SCHEMA ops FROM PUBLIC;
GRANT USAGE ON SCHEMA ops TO app_logger;
GRANT SELECT, INSERT, UPDATE ON ops.query_traces TO app_logger;
