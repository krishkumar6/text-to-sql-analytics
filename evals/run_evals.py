"""Run the eval set through the full pipeline and write a results report.

    python -m evals.run_evals                      # full run -> evals/REPORT.md and the README table
    python -m evals.run_evals --category simple    # a subset (report saved under evals/results/ only)
    python -m evals.run_evals --only arr mau
    python -m evals.run_evals --resume RUN_ID      # continue a run stopped by a rate limit / quota
    python -m evals.run_evals --report RUN_ID      # rebuild the report from saved results
    python -m evals.run_evals --check-gold         # execute the reference SQL only (no LLM, free)

Each case is appended to evals/results/<run_id>/cases.jsonl as soon as it finishes, so a run that hits a
provider quota can be resumed without re-spending tokens on finished cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import ROOT, Settings
from app.llm import LLMRateLimited
from app.main import build_pipeline
from app.pipeline import Pipeline
from app.traces import TraceStore
from evals.grading import compare
from evals.report import build_report, headline_table, _group_table, CATEGORY_ORDER

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CASES_PATH = HERE / "cases.json"
README = ROOT / "README.md"
README_START, README_END = "<!-- eval-results:start -->", "<!-- eval-results:end -->"
PREVIEW_ROWS = 5


class QuotaExhausted(Exception):
    def __init__(self, wait_s: float | None) -> None:
        super().__init__(f"provider asked us to wait {wait_s:.0f}s" if wait_s else "still rate-limited")
        self.wait_s = wait_s


# ---- helpers -------------------------------------------------------------------------------------

def load_cases() -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    for c in cases:
        c.setdefault("expect", "rows")
    return cases


def dataset_sha() -> str:
    return hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()[:12]


def fingerprint(pipeline: Pipeline) -> dict[str, list]:
    """Row count + content hash of every table, to prove write requests changed nothing."""
    q = pipeline.db.quote_identifier
    out = {}
    for t in pipeline.catalog.tables:
        r = pipeline.db.execute(
            f"SELECT count(*), coalesce(sum(hashtext(t::text)::bigint), 0) FROM {q(t.schema)}.{q(t.name)} t"
        )
        out[t.qualified] = r.rows[0]
    return out


def ask_with_backoff(pipeline: Pipeline, question: str, max_wait: float):
    for _ in range(6):
        try:
            return pipeline.ask(question)
        except LLMRateLimited as exc:
            wait = (exc.retry_after or 20) + 1
            if wait > max_wait:
                raise QuotaExhausted(wait) from exc
            print(f"      rate-limited, waiting {wait:.0f}s…", flush=True)
            time.sleep(wait)
    raise QuotaExhausted(None)


def run_case(pipeline: Pipeline, case: dict, max_wait: float) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": case["id"], "category": case["category"], "difficulty": case["difficulty"],
        "expect": case["expect"], "question": case["question"], "gold_sql": case.get("gold_sql"),
        "passed": False, "reason": "", "status": "exception", "attempts": None, "confidence": None,
        "latency_ms": 0, "input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "sql": None, "error": None,
        "got_preview": None, "gold_preview": None,
    }
    gold = pipeline.db.execute(case["gold_sql"], max_rows=5000) if case["expect"] == "rows" else None
    before = fingerprint(pipeline) if case["expect"] == "no_write" else None

    try:
        out = ask_with_backoff(pipeline, case["question"], max_wait)
    except QuotaExhausted:
        raise
    except Exception as exc:  # an API/LLM failure is a failed case, not a crashed run
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["reason"] = "the pipeline raised an exception"
        out = None

    if out is not None:
        u = out.usage
        record.update(
            status=out.status, attempts=out.attempts or None, confidence=out.confidence,
            latency_ms=out.latency_ms, input_tokens=u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            output_tokens=u.output_tokens, llm_calls=u.llm_calls, sql=out.sql, error=out.error,
        )

    if case["expect"] == "rows":
        record["gold_preview"] = gold.rows[:PREVIEW_ROWS]
        if out is None:
            pass
        elif out.status != "ok":
            record["reason"] = f"status was '{out.status}', expected an answer"
        elif out.truncated:
            record["reason"] = "result hit the row cap"
        else:
            record["got_preview"] = out.rows[:PREVIEW_ROWS]
            record["passed"], record["reason"] = compare(gold.columns, gold.rows, out.columns, out.rows)
    elif case["expect"] == "clarification":
        record["passed"] = out is not None and out.status == "clarification"
        record["reason"] = "abstained / asked for clarification" if record["passed"] else \
            f"answered with status '{record['status']}' instead of saying the data can't answer it"
    elif case["expect"] == "no_write":
        unchanged = fingerprint(pipeline) == before
        record["passed"] = unchanged
        record["reason"] = f"database unchanged (status '{record['status']}')" if unchanged else \
            "DATABASE CHANGED"
    return record


def write_report(run_dir: Path, meta: dict, records: list[dict], total: int, publish: bool) -> Path:
    order = {c["id"]: i for i, c in enumerate(load_cases())}
    records = sorted(records, key=lambda r: order.get(r["id"], 1e9))
    report = build_report(meta, records, total)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    if publish and records:  # a partial run is published too; its title and README line say so
        (HERE / "REPORT.md").write_text(report, encoding="utf-8")
        update_readme(meta, records, total)
        return HERE / "REPORT.md"
    return run_dir / "report.md"


def update_readme(meta: dict, records: list[dict], total: int) -> None:
    text = README.read_text(encoding="utf-8")
    if README_START not in text:
        return
    coverage = f"{len(records)} cases" if len(records) == total else f"**partial: {len(records)}/{total} cases**"
    block = "\n".join([
        README_START,
        f"Latest run: `{meta['model']}` via {meta['provider']}, {meta['started_at'][:10]}, {coverage}. "
        "Per-case table and failure analysis: [evals/REPORT.md](evals/REPORT.md).",
        "",
        *headline_table(meta, records),
        "",
        *_group_table(records, "category", CATEGORY_ORDER, "Category"),
        README_END,
    ])
    text = re.sub(re.escape(README_START) + ".*?" + re.escape(README_END), lambda _: block, text, flags=re.S)
    README.write_text(text, encoding="utf-8")


def load_run(run_id: str) -> tuple[Path, dict, list[dict]]:
    run_dir = RESULTS / run_id
    if not run_dir.exists():
        sys.exit(f"No saved run {run_id!r} in {RESULTS}")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    path = run_dir / "cases.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.exists() else []
    return run_dir, meta, records


# ---- main ----------------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--category", nargs="*", help="categories to run")
    parser.add_argument("--resume", metavar="RUN_ID")
    parser.add_argument("--report", metavar="RUN_ID")
    parser.add_argument("--check-gold", action="store_true")
    parser.add_argument("--max-wait", type=float, default=900,
                        help="longest rate-limit wait (s) before stopping so the run can be resumed later")
    args = parser.parse_args()
    logging.disable(logging.INFO)

    all_cases = load_cases()
    cases = all_cases
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    if args.category:
        cases = [c for c in cases if c["category"] in args.category]
    publish = not args.only and not args.category

    if args.report:  # built from saved results only; needs neither the database nor the LLM
        run_dir, meta, records = load_run(args.report)
        print(f"Report: {write_report(run_dir, meta, records, meta['total_cases'], meta['publish'])}")
        return

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    pipeline.traces = TraceStore("")  # keep eval traffic out of the production trace log

    if args.check_gold:
        for c in cases:
            if c["expect"] == "rows":
                r = pipeline.db.execute(c["gold_sql"])
                print(f"{c['id']:<26} {r.row_count:>3} rows  {r.rows[:2]}")
            else:
                print(f"{c['id']:<26} expect {c['expect']}")
        return

    if args.resume:
        run_dir, meta, records = load_run(args.resume)
        if meta["dataset_sha"] != dataset_sha() or meta["model"] != settings.model:
            sys.exit("Dataset or model changed since that run started; start a fresh run instead.")
        cases = [c for c in all_cases if c["id"] in set(meta["case_ids"])]
    else:
        started = datetime.now()
        run_id = f"{started:%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', settings.model.lower()).strip('-')}"
        run_dir = RESULTS / run_id
        run_dir.mkdir(parents=True)
        meta = {
            "run_id": run_id, "started_at": started.isoformat(timespec="seconds"), "provider": settings.provider,
            "model": settings.model, "sql_effort": settings.sql_effort, "summary_effort": settings.summary_effort,
            "dataset_sha": dataset_sha(), "case_ids": [c["id"] for c in cases], "total_cases": len(cases),
            "publish": publish,
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        records = []

    done = {r["id"] for r in records}
    todo = [c for c in cases if c["id"] not in done]
    baseline = fingerprint(pipeline)
    print(f"Run {meta['run_id']}: {len(todo)} of {meta['total_cases']} cases to go on {settings.model}\n")

    with (run_dir / "cases.jsonl").open("a", encoding="utf-8") as sink:
        for case in todo:
            try:
                record = run_case(pipeline, case, args.max_wait)
            except QuotaExhausted as exc:
                path = write_report(run_dir, meta, records, meta["total_cases"], meta["publish"])
                print(f"\nStopped: {exc}. {len(records)}/{meta['total_cases']} cases saved; partial report: {path}")
                print(f"Resume later with:  python -m evals.run_evals --resume {meta['run_id']}")
                sys.exit(2)
            records.append(record)
            sink.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            sink.flush()
            print(f"{'PASS' if record['passed'] else 'FAIL'}  {record['id']:<26} {record['category']:<16} "
                  f"{record['status']:<13} {record['latency_ms'] / 1000:5.1f}s  {record['reason'] if not record['passed'] else ''}",
                  flush=True)

    meta["db_unchanged"] = fingerprint(pipeline) == baseline
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    path = write_report(run_dir, meta, records, meta["total_cases"], meta["publish"])
    print()
    print("\n".join(headline_table(meta, records)))
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
