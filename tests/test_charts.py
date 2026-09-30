from app.charts import build_figure, infer_chart
from app.db import QueryResult
from app.llm import ChartSpec


def result(columns, rows):
    return QueryResult(sql="", columns=columns, rows=rows, truncated=False, elapsed_ms=1)


TIME_SERIES = result(["month", "mrr"], [["2026-07-01T00:00:00", 100.0], ["2026-08-01T00:00:00", 120.0],
                                        ["2026-09-01T00:00:00", 150.0]])
CATEGORIES = result(["plan", "customers"], [["Starter", 10], ["Pro", 7], ["Business", 2]])


def test_infers_line_for_time_series():
    assert infer_chart(TIME_SERIES).kind == "line"


def test_infers_bar_for_categories():
    spec = infer_chart(CATEGORIES)
    assert (spec.kind, spec.x, spec.y) == ("bar", "plan", ["customers"])


def test_single_value_has_no_chart():
    fig, spec = build_figure(None, result(["total"], [[42]]))
    assert fig is None and spec.kind == "none"


def test_uses_valid_llm_spec():
    fig, spec = build_figure(ChartSpec(kind="pie", x="plan", y=["customers"], series="", title="Mix"), CATEGORIES)
    assert spec.kind == "pie" and fig["data"][0]["type"] == "pie"


def test_falls_back_when_llm_spec_names_missing_column():
    fig, spec = build_figure(ChartSpec(kind="bar", x="nope", y=["customers"], series="", title=""), CATEGORIES)
    assert spec.kind == "bar" and spec.x == "plan" and fig is not None


def test_empty_result_has_no_chart():
    fig, _ = build_figure(ChartSpec(kind="bar", x="plan", y=["customers"], series="", title=""),
                          result(["plan", "customers"], []))
    assert fig is None
