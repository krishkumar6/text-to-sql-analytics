"""Schema introspection: builds the context the LLM sees and the table allowlist the guard enforces."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field

from sqlalchemy import inspect

from app.db import Database

MAX_SAMPLE_VALUE_CHARS = 60
LOW_CARDINALITY_LIMIT = 25  # text columns with at most this many distinct values list them all


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    comment: str | None = None
    primary_key: bool = False
    references: str | None = None  # "table.column"
    values: list[str] = field(default_factory=list)  # full value set of low-cardinality columns


@dataclass
class TableInfo:
    schema: str
    name: str
    comment: str | None
    columns: list[ColumnInfo]
    row_estimate: int
    example_columns: list[str] = field(default_factory=list)
    example_rows: list[list] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)


class SchemaCatalog:
    def __init__(self, tables: list[TableInfo]) -> None:
        self.tables = tables
        self._by_name: dict[str, TableInfo] = {}
        for t in tables:
            self._by_name[t.qualified.lower()] = t
            if t.schema == "public":
                self._by_name[t.name.lower()] = t

    @property
    def allowed_tables(self) -> set[str]:
        return set(self._by_name)

    def get(self, name: str) -> TableInfo | None:
        return self._by_name.get(name.lower())

    @classmethod
    def load(cls, db: Database, schemas: tuple[str, ...], sample_rows: int = 3) -> SchemaCatalog:
        insp = inspect(db.engine)
        row_estimates = _row_estimates(db, schemas)
        column_values = _low_cardinality_values(db, schemas)
        tables: list[TableInfo] = []
        for schema in schemas:
            for name in sorted(insp.get_table_names(schema=schema) + insp.get_view_names(schema=schema)):
                pk = set(insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
                fks = {}
                for fk in insp.get_foreign_keys(name, schema=schema):
                    for col, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
                        fks[col] = f"{fk['referred_table']}.{ref}"
                columns = [
                    ColumnInfo(
                        name=c["name"],
                        type=str(c["type"]).lower(),
                        nullable=bool(c["nullable"]),
                        comment=c.get("comment"),
                        primary_key=c["name"] in pk,
                        references=fks.get(c["name"]),
                        values=column_values.get((schema, name, c["name"]), []),
                    )
                    for c in insp.get_columns(name, schema=schema)
                ]
                table = TableInfo(
                    schema=schema,
                    name=name,
                    comment=insp.get_table_comment(name, schema=schema).get("text"),
                    columns=columns,
                    row_estimate=row_estimates.get((schema, name), 0),
                )
                if sample_rows > 0:
                    sample = db.execute(
                        f"SELECT * FROM {db.quote_identifier(schema)}.{db.quote_identifier(name)} LIMIT {int(sample_rows)}"
                    )
                    table.example_columns = sample.columns
                    table.example_rows = [[_clip(v) for v in row] for row in sample.rows]
                tables.append(table)
        return cls(tables)

    def render(self) -> str:
        """Compact, LLM-friendly description of every table."""
        parts = []
        for t in self.tables:
            lines = [f"## {t.qualified} (~{t.row_estimate:,} rows)"]
            if t.comment:
                lines.append(t.comment)
            for c in t.columns:
                line = f"- {c.name} {c.type}"
                flags = []
                if c.primary_key:
                    flags.append("PK")
                if c.references:
                    flags.append(f"FK -> {c.references}")
                if not c.nullable and not c.primary_key:
                    flags.append("not null")
                if flags:
                    line += f" [{', '.join(flags)}]"
                if c.comment:
                    line += f" -- {c.comment}"
                if c.values:
                    line += f" | values: {', '.join(c.values)}"
                lines.append(line)
            if t.example_rows:
                lines.append("Example rows:")
                lines.append(" | ".join(t.example_columns))
                lines.extend(" | ".join("NULL" if v is None else str(v) for v in row) for row in t.example_rows)
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def to_dict(self) -> list[dict]:
        return [{**asdict(t), "qualified": t.qualified} for t in self.tables]


def _row_estimates(db: Database, schemas: tuple[str, ...]) -> dict[tuple[str, str], int]:
    result = db.execute(
        """
        SELECT n.nspname, c.relname, GREATEST(c.reltuples, 0)::bigint
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p', 'm')
        """,
        {"schemas": list(schemas)},
    )
    return {(s, t): int(n) for s, t, n in result.rows}


def _low_cardinality_values(db: Database, schemas: tuple[str, ...]) -> dict[tuple[str, str, str], list[str]]:
    """Distinct values of low-cardinality text columns, from planner statistics (no table scans).

    pg_stats only shows rows for tables the current role can SELECT, so this respects grants.
    """
    result = db.execute(
        """
        SELECT s.schemaname, s.tablename, s.attname, s.most_common_vals::text
        FROM pg_stats s
        JOIN information_schema.columns c
          ON c.table_schema = s.schemaname AND c.table_name = s.tablename AND c.column_name = s.attname
        WHERE s.schemaname = ANY(:schemas)
          AND c.data_type IN ('text', 'character varying', 'character')
          AND s.n_distinct > 0 AND s.n_distinct <= :limit
          AND s.most_common_vals IS NOT NULL
        """,
        {"schemas": list(schemas), "limit": LOW_CARDINALITY_LIMIT},
    )
    return {(s, t, c): sorted(_parse_pg_array(vals)) for s, t, c, vals in result.rows}


def _parse_pg_array(literal: str) -> list[str]:
    inner = literal.strip()[1:-1]
    if not inner:
        return []
    return next(csv.reader([inner], quotechar='"', escapechar="\\"))


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_SAMPLE_VALUE_CHARS:
        return value[: MAX_SAMPLE_VALUE_CHARS - 1] + "…"
    return value
