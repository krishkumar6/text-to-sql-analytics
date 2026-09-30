"""Turn a query result plus a chart spec into a Plotly figure (as JSON for plotly.js)."""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
import plotly.express as px

from app.db import QueryResult
from app.llm import ChartSpec

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
MAX_CATEGORIES = 60


def infer_chart(result: QueryResult) -> ChartSpec:
    """Deterministic fallback when the LLM's spec is missing or unusable."""
    none = ChartSpec(kind="none", x="", y=[], series="", title="")
    if result.row_count < 2:
        return none
    df = _frame(result)
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not c.endswith("_id")]
    temporal = [c for c in df.columns if _is_temporal(df[c])]
    categorical = [c for c in df.columns if c not in numeric and c not in temporal]
    if temporal and numeric:
        return ChartSpec(kind="line", x=temporal[0], y=numeric[:3], series="", title="")
    if categorical and numeric and result.row_count <= MAX_CATEGORIES:
        return ChartSpec(kind="bar", x=categorical[0], y=numeric[:2], series="", title="")
    if len(numeric) >= 2:
        return ChartSpec(kind="scatter", x=numeric[0], y=[numeric[1]], series="", title="")
    return none


def build_figure(spec: ChartSpec | None, result: QueryResult) -> tuple[dict[str, Any] | None, ChartSpec]:
    """Return (plotly figure JSON or None, the spec actually used)."""
    if result.row_count == 0:
        return None, ChartSpec(kind="none", x="", y=[], series="", title="")
    if spec is None or not _spec_fits(spec, result):
        spec = infer_chart(result)
    if spec.kind == "none":
        return None, spec

    df = _frame(result)
    y = [c for c in spec.y if c in df.columns]
    series = spec.series if spec.series in df.columns and len(y) == 1 else None
    title = spec.title or None
    labels = {c: c.replace("_", " ") for c in df.columns}

    if spec.kind == "line":
        df = df.sort_values(spec.x)
        fig = px.line(df, x=spec.x, y=y, color=series, markers=len(df) <= 60, title=title, labels=labels)
    elif spec.kind == "bar":
        fig = px.bar(df, x=spec.x, y=y, color=series, barmode="group", title=title, labels=labels)
    elif spec.kind == "scatter":
        fig = px.scatter(df, x=spec.x, y=y[0], color=series, title=title, labels=labels)
    else:  # pie
        fig = px.pie(df, names=spec.x, values=y[0], title=title, labels=labels, hole=0.45)

    fig.update_layout(
        template="plotly_white",
        margin={"l": 48, "r": 16, "t": 48 if title else 16, "b": 40},
        legend_title_text="",
        font={"family": "Inter, system-ui, sans-serif", "size": 13},
    )
    if len(y) > 1 and spec.kind in ("line", "bar"):
        fig.update_layout(yaxis_title="")
    return json.loads(fig.to_json()), spec


def _spec_fits(spec: ChartSpec, result: QueryResult) -> bool:
    if spec.kind == "none":
        return True
    cols = set(result.columns)
    if spec.x not in cols or not spec.y or not all(c in cols for c in spec.y):
        return False
    df = _frame(result)
    if not all(pd.api.types.is_numeric_dtype(df[c]) for c in spec.y):
        return False
    if spec.kind == "pie":
        return result.row_count <= 8 and len(spec.y) == 1
    if spec.kind == "bar":
        return df[spec.x].nunique() <= MAX_CATEGORIES
    return True


def _frame(result: QueryResult) -> pd.DataFrame:
    df = pd.DataFrame(result.rows, columns=result.columns)
    for col in df.columns:
        if _is_temporal(df[col]):
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _is_temporal(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    sample = series.dropna().head(20)
    return len(sample) > 0 and all(isinstance(v, str) and ISO_DATE.match(v) for v in sample)
