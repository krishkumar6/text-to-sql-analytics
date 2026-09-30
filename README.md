# Text-to-SQL Analytics

Ask a business question in plain English, like "what was our MRR last month?" or "which plan
churns the most?", and get back the SQL it ran, the result table, a chart and a one-line answer.

I built this because most "chat with your database" demos quietly assume the model will behave.
I wanted one I'd actually be comfortable pointing at a real company database, and one where I could
tell you how often it's right with a number instead of a feeling.

Stack: FastAPI, PostgreSQL, sqlglot, Plotly, and either Groq (`gpt-oss-120b`) or Claude as the LLM,
switchable in config.

## What it does

1. At startup it reads the database schema: tables, columns, keys, comments, a few example values,
   and a short notes file that defines business metrics like MRR, churn and activation.
2. Your question goes to the LLM with that context. The model answers in structured JSON, not free
   text: the SQL, why it wrote it, how confident it is, and whether it needs to ask you something first.
3. The SQL is checked before it runs.
4. It runs as a read-only database user, with a timeout and a row cap.
5. If the check or the database rejects the query, the exact error goes back to the model for one
   repair attempt.
6. The result becomes a table, a chart and a one-sentence summary.
7. Every question is logged with its SQL, time spent in each step and tokens used. A thumbs-down
   from a user becomes a new test case.

The demo database is a made-up SaaS company with 12 months of data: 4,000 users, 5,000
subscriptions, 4,000 orders and 300,000 product events.

## What I did differently

**Safety doesn't depend on the prompt.** Telling a model "only write SELECT queries" isn't
security, so there are three separate layers:

- The app connects as a Postgres role that can only read. Even if bad SQL slips through, it can't
  write anything or see the internal logging tables.
- Every query runs inside a read-only transaction with a timeout and is always rolled back.
- Before anything runs, sqlglot parses the SQL and rejects multiple statements, writes hidden inside
  a CTE, `SELECT INTO`, tables that aren't on the allowlist, and functions like `pg_sleep`,
  `pg_read_file` or `dblink`. It adds a `LIMIT` if one is missing.

One test turns the SQL check off on purpose and confirms the database still refuses the write.

**It can say "I don't know".** If a question is ambiguous, or asks about data that doesn't exist
(NPS, sales reps, CAC), it asks a clarifying question instead of making up a confident wrong number.

**It checks real values before filtering.** The model has one tool that looks up the actual values
in a column. It filters on `'pro'` because that's what's in the table, not on `'Pro'` because it
sounds right.

**Accuracy is measured, not eyeballed.** There are 39 test questions across 8 categories: simple
counts, business metrics, time series, joins and rankings, value lookups, multi-step cohort queries,
questions it should refuse to answer, and "please delete the users" style requests. A generated query
passes only if it returns the same data as a hand-written reference query. Column names, column
order, row order and small rounding differences are ignored. For the safety cases, the check is that
every table's contents are unchanged afterwards.

The report also shows how often the first attempt was already right, whether the model's "high
confidence" actually means it's right more often, and the latency and token cost of each question.

<!-- eval-results:start -->
Latest run: `openai/gpt-oss-120b` via groq, 2026-09-30, **partial: 38/39 cases**. Per-case table and failure analysis: [evals/REPORT.md](evals/REPORT.md).

| Metric | Result |
|---|---|
| **Execution accuracy** (SQL questions) | **33/34 = 97%** |
| First-attempt accuracy (no repair) | 33/34 = 97% |
| Repair loop | 0 retried, 0 rescued |
| Abstains on unanswerable questions | 3/3 |
| Safety: database unchanged after write requests | 1/1 |
| **Overall** | **37/38 = 97%** |
| Latency p50 / p95 | 17.6 s / 29.8 s |
| Tokens per question (in / out) | 3,322 / 594 (1.9 LLM calls) |

| Category | Cases | Passed | Accuracy |
|---|---:|---:|---:|
| simple | 5 | 5 | 100% |
| business_metric | 11 | 11 | 100% |
| time_series | 7 | 7 | 100% |
| join_ranking | 6 | 5 | 83% |
| value_lookup | 2 | 2 | 100% |
| multi_step | 3 | 3 | 100% |
| unanswerable | 3 | 3 | 100% |
| safety | 1 | 1 | 100% |
<!-- eval-results:end -->

The one miss is a hard question about customers upgrading from one paid plan to a higher one. The
last safety case hasn't run yet because Groq's free-tier daily quota ran out. The eval saves progress
after every case, so it picks up where it stopped.

## What I optimized

- **Accuracy, by fixing context rather than code.** In an earlier 14-question run the model scored
  12/14. Both misses were the same mistake: comparing a timestamp to a bare date, which silently drops
  the last day of the range. One line in the business notes file (`db/business_context.md`) fixed it.
  Most of the accuracy gains came from editing that file.
- **Cost and latency you can predict.** It's a fixed pipeline, not an open-ended agent. A question
  takes about 2 LLM calls and around 4k tokens, and every step is timed.
- **Less prompt work per question.** The schema context is built once at startup from Postgres
  catalog statistics, without scanning tables, and sent as a cached system prompt.
- **One retry, with the real error.** When a query fails, the model sees the actual Postgres error
  message. It never loops more than once.
- **Partial answers still help.** If the summary call fails, you still get the table and a chart
  picked by a simple rule. Provider outages and rate limits come back as clean 503 and 429 errors
  instead of crashes.
- **Tests are free to run.** The LLM is swapped for scripted fakes, so all 75 tests cost nothing and
  give the same result every time.

## Run it

Needs Docker and Python 3.12+.

```bash
docker compose up -d db
pip install -r requirements.txt
python -m scripts.seed
cp .env.example .env        # add GROQ_API_KEY, or ANTHROPIC_API_KEY with LLM_PROVIDER=anthropic
uvicorn app.main:app --reload
```

Then open http://localhost:8000. Run the tests with `pytest` and the eval with
`python -m evals.run_evals` (`--resume RUN_ID` continues a run that hit a rate limit).

## Deploy

The public demo runs on Render (the app, from the Dockerfile) and Neon (Postgres), both on free tiers.

1. Create a Neon project in the Singapore region and copy its connection string.
2. Create the roles, the trace table and the demo data in it:
   ```bash
   DATABASE_ADMIN_URL="postgresql://<owner>:<password>@<host>/<db>?sslmode=require" python -m scripts.setup_db
   ```
   This gives the two app roles random passwords, checks that the read-only role really can't write,
   and prints `DATABASE_URL` and `TRACE_DATABASE_URL`.
3. On Render, create a Blueprint from this repo ([render.yaml](render.yaml)) and paste those two URLs
   and a `GROQ_API_KEY` when asked.

Since a public demo spends my LLM quota, it has limits: 8 questions per visitor per hour and 40 per
day overall (set in `render.yaml`). The trace log at `/traces` needs the `X-Admin-Token` header, and
Render generates that value. The free instance sleeps when idle, so the first visit takes about 30–60
seconds to wake it.

## What's next

- Follow-up questions, like "now split that by plan".
- For large databases, sending only the relevant tables instead of the whole schema.
- Login and per-user row-level security.
- Prompt-injection cases in the eval set.
