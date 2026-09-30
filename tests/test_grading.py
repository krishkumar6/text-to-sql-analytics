"""The grader decides the headline number, so it gets its own tests."""

from evals.grading import column_matches, compare
from evals.report import build_report, summary


def test_ignores_column_names_order_and_extra_columns():
    ok, _ = compare(["plan", "mrr"], [["Pro", 10.0], ["Starter", 5.0]],
                    ["current_mrr", "plan_name", "customers"], [[5.0, "Starter", 3], [10.0, "Pro", 7]])
    assert ok


def test_row_count_must_match():
    ok, reason = compare(["n"], [[1], [2]], ["n"], [[1]])
    assert not ok and "row count" in reason


def test_wrong_value_fails_with_column_name():
    ok, reason = compare(["plan", "mrr"], [["Pro", 10.0]], ["plan", "mrr"], [["Pro", 11.0]])
    assert not ok and "'mrr'" in reason


def test_numeric_tolerance_and_percent_scaling():
    assert column_matches([25.7], [25.66])
    assert column_matches([25.7], [0.257])
    assert not column_matches([25.7], [26.5])
    assert column_matches([4000], [4000.0])


def test_month_labels_match_across_formats():
    gold = ["2026-08-01T00:00:00", "2026-09-01T00:00:00"]
    assert column_matches(gold, ["2026-08", "2026-09"])
    assert column_matches(gold, ["2026-09-30", "2026-08-31"])
    assert not column_matches(gold, ["2026-07", "2026-08"])


def test_day_level_dates_are_compared_by_day():
    assert not column_matches(["2026-08-05", "2026-08-06"], ["2026-08-01", "2026-08-02"])


def test_text_is_case_insensitive_and_nulls_must_line_up():
    assert column_matches(["Pro", "Starter"], ["starter", "PRO"])
    assert not column_matches([None, "Pro"], ["Pro", "Starter"])


def _record(**kw):
    base = {"id": "x", "category": "simple", "difficulty": "easy", "expect": "rows", "question": "q",
            "passed": True, "reason": "match", "status": "ok", "attempts": 1, "confidence": "high",
            "latency_ms": 1000, "input_tokens": 100, "output_tokens": 10, "llm_calls": 2, "sql": "SELECT 1",
            "gold_sql": "SELECT 1", "error": None, "got_preview": [[1]], "gold_preview": [[1]]}
    return {**base, **kw}


def test_summary_separates_first_attempt_and_repairs():
    records = [_record(id="a"), _record(id="b", attempts=2), _record(id="c", attempts=2, passed=False),
               _record(id="d", expect="clarification", status="clarification", attempts=None)]
    s = summary({}, records)
    assert (s["sql_total"], s["sql_passed"], s["sql_first_attempt"]) == (3, 2, 1)
    assert (s["repairs"], s["repairs_rescued"]) == (2, 1)
    assert (s["abstain_total"], s["abstain_passed"]) == (1, 1)


def test_report_lists_failures():
    meta = {"model": "m", "provider": "p", "sql_effort": "medium", "summary_effort": "low", "run_id": "r",
            "started_at": "2026-09-30T00:00:00", "dataset_sha": "abc"}
    report = build_report(meta, [_record(id="good"), _record(id="bad", passed=False, reason="row count 1")], 2)
    assert "## Failures" in report and "`bad`" in report and "row count 1" in report
    assert "partial" not in report
