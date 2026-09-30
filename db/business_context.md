# Business context

Sent to the LLM with every question. This is the semantic layer: metric definitions and
company conventions live here so the model uses them instead of inventing its own.

## Data coverage
- 12 months of data: 2025-10-01 through 2026-09-30. Interpret "last month", "this quarter",
  "recently" against the latest date in the data (September 2026), and say which dates you used.

## Metric definitions
- **Paid subscription**: a subscription whose plan has `monthly_price > 0` (i.e. not Free).
- **Currently subscribed**: `subscriptions.status IN ('active', 'past_due')`. The status alone defines
  "current"; don't add date filters to it.
- **Subscribed at the end of day D**: `started_at < D + interval '1 day' AND (canceled_at IS NULL OR canceled_at >= D + interval '1 day')`.
  For the end of a month use the first day of the next month as the cutoff.
- All `*_at` columns are timestamps. Never compare them to a bare date with `<=`: `started_at <= '2026-09-30'`
  means midnight and silently drops the rest of that day. Use half-open ranges: `>= start AND < next_day`.
- **MRR**: `SUM(subscriptions.mrr)` over paid subscriptions subscribed at the point in time asked about.
  "Current MRR" uses currently subscribed rows. **ARR** = MRR × 12.
- **Paying customers**: distinct `user_id` with a paid subscription at that point in time.
- **ARPU / ARPA**: MRR / paying customers.
- **Revenue**: `SUM(orders.amount)` where `orders.status = 'paid'`, bucketed by `orders.created_at`.
  Refunded and failed orders are not revenue.
- **Churn**: a paid subscription canceled with `cancel_reason` other than `'upgrade'`/`'downgrade'`
  (plan changes are not churn). **Monthly logo churn rate** = churned paid subscriptions in the month /
  paid subscriptions subscribed at the start of that month.
- **Free-to-paid conversion**: users with at least one paid subscription / all users in the signup cohort.
- **Active user** (DAU/WAU/MAU): distinct users with at least one event in the day/week/month.
- **Activated user**: a user with a `project_created` event within 7 days of `users.created_at`.

## Conventions
- Money is USD. Round money to 2 decimals and rates/percentages to 1 decimal.
- Bucket time series with `date_trunc('month', ...)` (or week/day); weeks start on Monday.
- Plans in tier order: Free (0), Starter (1), Pro (2), Business (3).
