"""Markdown report for an eval run."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

CATEGORY_ORDER = ["simple", "business_metric", "time_series", "join_ranking", "value_lookup", "multi_step",
                  "unanswerable", "safety"]
DIFFICULTY_ORDER = ["easy", "medium", "hard"]
CONFIDENCE_ORDER = ["high", "medium", "low"]


def _pct(passed: int, total: int) -> str:
    return f"{passed / total:.0%}" if total else "–"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def _group_table(records: list[dict], key: str, order: list[str], header: str) -> list[str]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r.get(key) or "—"].append(r)
    keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    lines = [f"| {header} | Cases | Passed | Accuracy |", "|---|---:|---:|---:|"]
    for k in keys:
        rs = groups[k]
        p = sum(r["passed"] for r in rs)
        lines.append(f"| {k} | {len(rs)} | {p} | {_pct(p, len(rs))} |")
    return lines


def summary(meta: dict, records: list[dict]) -> dict[str, Any]:
    sql_cases = [r for r in records if r["expect"] == "rows"]
    abstain = [r for r in records if r["expect"] == "clarification"]
    safety = [r for r in records if r["expect"] == "no_write"]
    answered = [r for r in records if r["status"] != "exception"]
    repaired = [r for r in sql_cases if (r["attempts"] or 0) > 1]
    return {
        "total": len(records),
        "passed": sum(r["passed"] for r in records),
        "sql_total": len(sql_cases),
        "sql_passed": sum(r["passed"] for r in sql_cases),
        "sql_first_attempt": sum(r["passed"] and r["attempts"] == 1 for r in sql_cases),
        "repairs": len(repaired),
        "repairs_rescued": sum(r["passed"] for r in repaired),
        "abstain_total": len(abstain),
        "abstain_passed": sum(r["passed"] for r in abstain),
        "safety_total": len(safety),
        "safety_passed": sum(r["passed"] for r in safety),
        "latency_p50": _percentile([r["latency_ms"] for r in answered], 0.5) / 1000,
        "latency_p95": _percentile([r["latency_ms"] for r in answered], 0.95) / 1000,
        "tokens_in": sum(r["input_tokens"] for r in answered) / max(len(answered), 1),
        "tokens_out": sum(r["output_tokens"] for r in answered) / max(len(answered), 1),
        "llm_calls": sum(r["llm_calls"] for r in answered) / max(len(answered), 1),
    }


def headline_table(meta: dict, records: list[dict]) -> list[str]:
    s = summary(meta, records)
    return [
        "| Metric | Result |",
        "|---|---|",
        f"| **Execution accuracy** (SQL questions) | **{s['sql_passed']}/{s['sql_total']} = {_pct(s['sql_passed'], s['sql_total'])}** |",
        f"| First-attempt accuracy (no repair) | {s['sql_first_attempt']}/{s['sql_total']} = {_pct(s['sql_first_attempt'], s['sql_total'])} |",
        f"| Repair loop | {s['repairs']} retried, {s['repairs_rescued']} rescued |",
        f"| Abstains on unanswerable questions | {s['abstain_passed']}/{s['abstain_total']} |",
        f"| Safety: database unchanged after write requests | {s['safety_passed']}/{s['safety_total']} |",
        f"| **Overall** | **{s['passed']}/{s['total']} = {_pct(s['passed'], s['total'])}** |",
        f"| Latency p50 / p95 | {s['latency_p50']:.1f} s / {s['latency_p95']:.1f} s |",
        f"| Tokens per question (in / out) | {s['tokens_in']:,.0f} / {s['tokens_out']:,.0f} ({s['llm_calls']:.1f} LLM calls) |",
    ]


def build_report(meta: dict, records: list[dict], total_cases: int) -> str:
    done = len(records)
    title = "Eval report" + ("" if done == total_cases else f" (partial: {done}/{total_cases} cases)")
    sql_cases = [r for r in records if r["expect"] == "rows"]
    lines = [
        f"# {title}",
        "",
        f"- **Model:** `{meta['model']}` via {meta['provider']} (SQL effort `{meta['sql_effort']}`, "
        f"summary effort `{meta['summary_effort']}`)",
        f"- **Run:** `{meta['run_id']}`, started {meta['started_at']}",
        f"- **Dataset:** `evals/cases.json`, {total_cases} cases (sha256 `{meta['dataset_sha']}`)",
        f"- **Database unchanged across the whole run:** {'yes' if meta.get('db_unchanged', True) else '**NO**'}",
        "",
        "## Summary",
        "",
        *headline_table(meta, records),
        "",
        "## By category",
        "",
        *_group_table(records, "category", CATEGORY_ORDER, "Category"),
        "",
        "## By difficulty",
        "",
        *_group_table(records, "difficulty", DIFFICULTY_ORDER, "Difficulty"),
        "",
        "## Confidence calibration (SQL questions)",
        "",
        "Is the model's self-reported confidence informative? High-confidence answers should be right more often.",
        "",
        *_group_table(sql_cases, "confidence", CONFIDENCE_ORDER, "Confidence"),
        "",
        "## All cases",
        "",
        "| # | Case | Category | Difficulty | Result | Status | Attempts | Confidence | Latency | Tokens in/out |",
        "|---:|---|---|---|:---:|---|---:|---|---:|---:|",
    ]
    for i, r in enumerate(records, start=1):
        lines.append(
            f"| {i} | `{r['id']}` | {r['category']} | {r['difficulty']} | {'✅' if r['passed'] else '❌'} | "
            f"{r['status']} | {r['attempts'] or '–'} | {r['confidence'] or '–'} | "
            f"{r['latency_ms'] / 1000:.1f} s | {r['input_tokens']:,} / {r['output_tokens']:,} |"
        )

    failures = [r for r in records if not r["passed"]]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines += [
            f"### `{r['id']}` ({r['category']}, {r['difficulty']})",
            "",
            f"**Question:** {r['question']}",
            "",
            f"**Why it failed:** {r['reason']}",
            "",
        ]
        if r.get("error"):
            lines += [f"**Error:** `{r['error']}`", ""]
        if r.get("sql"):
            lines += ["Generated SQL:", "```sql", r["sql"].strip(), "```", ""]
        if r.get("gold_sql"):
            lines += ["Reference SQL:", "```sql", r["gold_sql"].strip(), "```", ""]
        if r.get("got_preview") is not None and r.get("gold_preview") is not None:
            lines += [
                f"Got (first rows): `{json.dumps(r['got_preview'], ensure_ascii=False)}`",
                "",
                f"Expected (first rows): `{json.dumps(r['gold_preview'], ensure_ascii=False)}`",
                "",
            ]
    return "\n".join(lines) + "\n"
