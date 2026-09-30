"""Static validation of LLM-generated SQL before it reaches the database.

This catches mistakes early and gives the model precise feedback. It is defence in depth, not the
security boundary: that is the analytics_ro role plus the read-only transaction in db.py.
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# sqlglot warns when it falls back to parsing a statement as a raw Command; we reject those anyway.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Statement/clause types that must never appear anywhere in the tree.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter, exp.TruncateTable,
    exp.Grant, exp.Copy, exp.Command, exp.Pragma, exp.Set, exp.Use, exp.Transaction, exp.Commit,
    exp.Rollback, exp.Attach, exp.Detach, exp.LoadData,
    exp.Into,  # SELECT ... INTO new_table
    exp.Lock,  # SELECT ... FOR UPDATE / FOR SHARE
)

# Functions that read files, sleep, run nested SQL, touch other sessions or change settings.
FORBIDDEN_FUNCTIONS = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "pg_file_write",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_from_bytea",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    "cursor_to_xml", "table_to_xml", "schema_to_xml", "database_to_xml",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "set_config", "pg_advisory_lock", "pg_advisory_xact_lock", "pg_try_advisory_lock",
    "pg_notify", "txid_current", "nextval", "setval",
})


class UnsafeSQLError(ValueError):
    """The SQL failed validation. The message is written to be fed back to the LLM."""


def validate_sql(
    sql: str,
    *,
    allowed_tables: set[str],
    max_rows: int,
    dialect: str = "postgres",
) -> str:
    """Return a normalised, LIMITed version of `sql`, or raise UnsafeSQLError.

    `allowed_tables` holds lower-case names, bare ("orders") and/or qualified ("public.orders").
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeSQLError("The SQL is empty.")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read=dialect) if s is not None]
    except ParseError as exc:
        raise UnsafeSQLError(f"Could not parse the SQL: {_first_line(exc)}") from exc

    if len(statements) != 1:
        raise UnsafeSQLError(f"Exactly one statement is allowed; got {len(statements)}.")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeSQLError(f"Only read-only SELECT queries are allowed; got {tree.key.upper()}.")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeSQLError(f"Forbidden construct in query: {node.key.upper()}.")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in FORBIDDEN_FUNCTIONS:
                raise UnsafeSQLError(f"Function {name}() is not allowed.")

    _check_tables(tree, allowed_tables)

    if tree.args.get("limit") is None:
        tree = tree.limit(max_rows)
    return tree.sql(dialect=dialect, pretty=True)


def _check_tables(tree: exp.Expression, allowed_tables: set[str]) -> None:
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue  # table-valued function such as generate_series(); checked as a function
        name = table.name.lower()
        schema = table.db.lower()
        if not schema and name in cte_names:
            continue
        qualified = f"{schema}.{name}" if schema else name
        if qualified not in allowed_tables:
            raise UnsafeSQLError(
                f"Table {qualified!r} is not available. Use only the tables listed in the schema."
            )


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:300]
