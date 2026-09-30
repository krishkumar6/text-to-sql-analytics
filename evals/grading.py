"""Execution-accuracy grading: does a generated result contain the same data as the reference?

Rules (deliberately lenient on presentation, strict on data):
- Row counts must be equal.
- Every reference column must match a distinct generated column by *values*; names and column order are
  ignored, extra generated columns are allowed (e.g. revenue plus an order count).
- Row order is ignored (compared as sorted multisets).
- Numbers match within 0.5% relative or 0.051 absolute (covers rounding to 1-2 decimals). A fraction also
  matches the same percentage (0.257 vs 25.7).
- Dates are compared by day; if every reference date is the first of a month, by month, so '2026-08',
  '2026-08-01' and '2026-08-31' all label August.
"""

from __future__ import annotations

import math
import re
from typing import Any

ISO_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _date_parts(v: Any) -> tuple[str, str, str] | None:
    if not isinstance(v, str):
        return None
    m = ISO_DATE.match(v.strip())
    return (m.group(1), m.group(2), m.group(3) or "01") if m else None


def _numbers_match(expected: list[float], actual: list[float]) -> bool:
    e, a = sorted(expected), sorted(actual)

    def close(xs, ys):
        return all(math.isclose(x, y, rel_tol=0.005, abs_tol=0.051) for x, y in zip(xs, ys))

    return close(e, a) or close(e, [y * 100 for y in a])


def column_matches(expected: list[Any], actual: list[Any]) -> bool:
    if len(expected) != len(actual):
        return False
    if sum(v is None for v in expected) != sum(v is None for v in actual):
        return False
    e = [v for v in expected if v is not None]
    a = [v for v in actual if v is not None]
    if not e:
        return True

    if all(_is_number(v) for v in e):
        return all(_is_number(v) for v in a) and _numbers_match([float(v) for v in e], [float(v) for v in a])

    e_dates = [_date_parts(v) for v in e]
    if all(e_dates):
        a_dates = [_date_parts(v) for v in a]
        if not all(a_dates):
            return False
        by_month = all(d[2] == "01" for d in e_dates)
        key = (lambda d: d[:2]) if by_month else (lambda d: d)
        return sorted(map(key, e_dates)) == sorted(map(key, a_dates))

    return sorted(str(v).strip().lower() for v in e) == sorted(str(v).strip().lower() for v in a)


def compare(gold_columns: list[str], gold_rows: list[list[Any]],
            pred_columns: list[str], pred_rows: list[list[Any]]) -> tuple[bool, str]:
    """Return (passed, reason). The reason explains a failure in one line."""
    if len(gold_rows) != len(pred_rows):
        return False, f"row count {len(pred_rows)}, expected {len(gold_rows)}"
    pred_cols = [[row[i] for row in pred_rows] for i in range(len(pred_columns))]
    unused = list(range(len(pred_cols)))
    for gi, name in enumerate(gold_columns):
        gold_col = [row[gi] for row in gold_rows]
        hit = next((pi for pi in unused if column_matches(gold_col, pred_cols[pi])), None)
        if hit is None:
            return False, f"no generated column matches the values of reference column '{name}'"
        unused.remove(hit)
    return True, "match"
