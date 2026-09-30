import pytest

from app.guard import UnsafeSQLError, validate_sql

ALLOWED = {"users", "orders", "plans", "subscriptions", "events",
           "public.users", "public.orders", "public.plans", "public.subscriptions", "public.events"}


def check(sql: str, max_rows: int = 100) -> str:
    return validate_sql(sql, allowed_tables=ALLOWED, max_rows=max_rows)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users",
    "select count(*) from public.orders where status = 'paid';",
    "WITH m AS (SELECT date_trunc('month', created_at) AS month FROM users) SELECT month, count(*) FROM m GROUP BY 1",
    "SELECT plan_id FROM subscriptions UNION ALL SELECT plan_id FROM plans",
    "SELECT d::date FROM generate_series('2026-01-01'::date, '2026-01-31', interval '1 day') AS d",
    "SELECT email FROM users WHERE email ILIKE '%@example.com'",
    "SELECT u.user_id, (SELECT count(*) FROM orders o WHERE o.user_id = u.user_id) FROM users u",
])
def test_allows_read_only_queries(sql):
    assert check(sql)


@pytest.mark.parametrize("sql, reason", [
    ("DROP TABLE users", "Only read-only SELECT"),
    ("DELETE FROM users", "Only read-only SELECT"),
    ("UPDATE plans SET monthly_price = 0", "Only read-only SELECT"),
    ("INSERT INTO plans VALUES (9, 'x', 9, 1, 1, 1)", "Only read-only SELECT"),
    ("TRUNCATE users", "Only read-only SELECT"),
    ("COPY users TO '/tmp/users.csv'", "Only read-only SELECT"),
    ("SET statement_timeout = 0", "Only read-only SELECT"),
    ("SELECT 1; DROP TABLE users", "Exactly one statement"),
    ("WITH gone AS (DELETE FROM users RETURNING *) SELECT * FROM gone", "DELETE"),
    ("SELECT * INTO backup FROM users", "INTO"),
    ("SELECT * FROM users FOR UPDATE", "LOCK"),
    ("SELECT pg_sleep(30)", "pg_sleep"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT query_to_xml('delete from users', true, true, '')", "query_to_xml"),
    ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
    ("SELECT * FROM pg_catalog.pg_authid", "not available"),
    ("SELECT * FROM ops.query_traces", "not available"),
    ("SELECT * FROM information_schema.tables", "not available"),
    ("SELECT * FROM secrets", "not available"),
    ("", "empty"),
])
def test_blocks_unsafe_or_unknown(sql, reason):
    with pytest.raises(UnsafeSQLError, match=reason):
        check(sql)


def test_adds_limit_when_missing():
    assert check("SELECT * FROM users", max_rows=50).rstrip().endswith("LIMIT 50")


def test_keeps_existing_limit():
    out = check("SELECT * FROM users LIMIT 5", max_rows=50)
    assert "LIMIT 5" in out and "LIMIT 50" not in out


def test_cte_names_are_not_treated_as_tables():
    assert "paid" in check("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT count(*) FROM paid")
