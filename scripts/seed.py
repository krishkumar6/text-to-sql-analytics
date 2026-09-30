"""Generate 12 months of deterministic SaaS demo data and load it into Postgres.

Run as the database owner, not the read-only role:
    python -m scripts.seed
"""

from __future__ import annotations

import calendar
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.getenv("DATABASE_ADMIN_URL", "postgresql://postgres:postgres@localhost:5434/analytics")

START = datetime(2025, 10, 1)
END = datetime(2026, 10, 1)  # exclusive
N_USERS = 4000
SEED = 42

# plan_id, name, tier_rank, monthly_price, annual_price, seat_limit
PLANS = [
    (1, "Free", 0, 0, 0, 1),
    (2, "Starter", 1, 19, 190, 3),
    (3, "Pro", 2, 49, 490, 10),
    (4, "Business", 3, 149, 1490, None),
]
PRICE = {p[0]: (p[3], p[4]) for p in PLANS}
MONTHLY_CHURN = {2: 0.055, 3: 0.035, 4: 0.02}
EVENTS_PER_DAY = {1: 0.8, 2: 1.2, 3: 2.5, 4: 4.0}
CONVERSION_BY_SOURCE = {"organic": 0.25, "paid_search": 0.22, "social": 0.15, "referral": 0.32, "partner": 0.35}

COUNTRIES = [("United States", 30), ("India", 18), ("United Kingdom", 10), ("Germany", 9), ("Canada", 6),
             ("Brazil", 6), ("France", 5), ("Australia", 5), ("Japan", 4), ("Netherlands", 4), ("Singapore", 3)]
SOURCES = [("organic", 35), ("paid_search", 25), ("social", 15), ("referral", 15), ("partner", 10)]
EVENT_NAMES = [("login", 30), ("dashboard_viewed", 28), ("project_created", 8), ("report_exported", 10),
               ("invite_sent", 5), ("integration_connected", 2), ("api_call", 17)]
PLATFORMS = [("web", 70), ("ios", 18), ("android", 12)]
CHURN_REASONS = [("too_expensive", 35), ("missing_features", 25), ("switched_competitor", 20), ("no_longer_needed", 20)]

FIRST = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Priya", "Lucas", "Emma", "Arjun", "Olivia", "Kenji", "Chloe",
         "Mateo", "Ananya", "Ethan", "Hannah", "Rohan", "Isabella", "Felix", "Zara", "Leo", "Mia", "Omar", "Nina",
         "Daniel", "Aisha", "Hugo", "Grace", "Vikram", "Lena"]
LAST = ["Sharma", "Smith", "Müller", "Silva", "Tanaka", "Brown", "Patel", "Martin", "Garcia", "Kumar", "Wilson",
        "Dubois", "Johnson", "Singh", "Rossi", "Nguyen", "Kim", "Andersson", "Taylor", "Iyer", "Lopez", "Novak",
        "Fischer", "Reddy", "Clark", "Moreau", "Das", "Walker", "Costa", "Mehta"]
COMPANY_WORDS = ["Nimbus", "Vertex", "Bluefin", "Quanta", "Orbit", "Lumen", "Cedar", "Pixel", "Harbor", "Ember",
                 "Atlas", "Nova", "Summit", "Ridge", "Kite", "Delta"]
COMPANY_SUFFIX = ["Labs", "Analytics", "Studio", "Systems", "Group", "Cloud", "Works", "AI"]


def weighted(rng: random.Random, options: list[tuple]) -> str:
    return rng.choices([o[0] for o in options], weights=[o[1] for o in options])[0]


def add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year, month = dt.year + month // 12, month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def random_between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    return start + timedelta(seconds=rng.random() * (end - start).total_seconds())


class Generator:
    def __init__(self, seed: int = SEED) -> None:
        self.rng = random.Random(seed)
        self.users: list[tuple] = []
        self.subscriptions: list[tuple] = []
        self.orders: list[tuple] = []
        self.events: list[tuple] = []

    # ---- users -------------------------------------------------------------------------------
    def make_users(self) -> None:
        rng = self.rng
        days = [START + timedelta(days=d) for d in range((END - START).days)]
        # Signups grow ~2.2x over the year and dip at weekends.
        weights = [(1 + 1.2 * i / len(days)) * (0.6 if d.weekday() >= 5 else 1.0) for i, d in enumerate(days)]
        signup_days = sorted(rng.choices(days, weights=weights, k=N_USERS))
        for user_id, day in enumerate(signup_days, start=1):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            company = f"{rng.choice(COMPANY_WORDS)} {rng.choice(COMPANY_SUFFIX)}" if rng.random() < 0.65 else None
            created_at = day + timedelta(seconds=rng.randint(0, 86399))
            self.users.append((
                user_id, f"{first}.{last}.{user_id}@example.com".lower(), f"{first} {last}", company,
                weighted(rng, COUNTRIES), weighted(rng, SOURCES), created_at,
            ))

    # ---- subscriptions, orders, events ---------------------------------------------------------
    def simulate(self) -> None:
        for user in self.users:
            self._simulate_user(user[0], user[5], user[6])

    def _new_subscription(self, user_id, plan_id, interval, started_at) -> list:
        monthly, annual = PRICE[plan_id]
        mrr = round(annual / 12, 2) if interval == "annual" else float(monthly)
        # Mutable until the end of the simulation: [id, user, plan, status, interval, mrr, start, end, reason]
        row = [len(self.subscriptions) + 1, user_id, plan_id, "active", interval, mrr, started_at, None, None]
        self.subscriptions.append(row)
        return row

    def _cancel(self, sub: list, at: datetime, reason: str) -> None:
        sub[3], sub[7], sub[8] = "canceled", at, reason

    def _order(self, user_id, sub_id, order_type, amount, created_at, status="paid") -> None:
        self.orders.append((len(self.orders) + 1, user_id, sub_id, order_type, amount, status, created_at))

    def _charge_amount(self, plan_id: int, interval: str) -> float:
        monthly, annual = PRICE[plan_id]
        return float(annual if interval == "annual" else monthly)

    def _simulate_user(self, user_id: int, source: str, created_at: datetime) -> None:
        rng = self.rng
        free = self._new_subscription(user_id, 1, "monthly", created_at)
        self._events(user_id, 1, created_at, END, free_user=True)

        if rng.random() >= CONVERSION_BY_SOURCE[source]:
            return
        convert_at = created_at + timedelta(days=max(0.5, rng.expovariate(1 / 12)))
        if convert_at >= END:
            return
        self._cancel(free, convert_at, "upgrade")

        plan = int(weighted(rng, [(2, 50), (3, 38), (4, 12)]))
        interval = "annual" if rng.random() < (0.35 if plan == 4 else 0.22) else "monthly"
        sub = self._new_subscription(user_id, plan, interval, convert_at)
        self._order(user_id, sub[0], "new", self._charge_amount(plan, interval), convert_at,
                    "refunded" if rng.random() < 0.01 else "paid")
        cursor = convert_at

        while True:
            period = 12 if interval == "annual" else 1
            next_bill = add_months(cursor, period)
            churn_p = MONTHLY_CHURN[plan] * (0.3 if interval == "annual" else 1)
            upgrade_p = 0.025 * period if plan < 4 else 0
            downgrade_p = 0.008 * period if plan > 2 else 0
            if plan >= 3 and rng.random() < 0.06:
                self._order(user_id, sub[0], "addon", float(rng.choice([15, 25, 50, 99])),
                            random_between(rng, cursor, min(next_bill, END)))

            r = rng.random()
            change_at = random_between(rng, cursor, next_bill)
            if r < churn_p + upgrade_p + downgrade_p and change_at < END:
                self._events(user_id, plan, sub[6], change_at)
                if r < churn_p:
                    self._cancel(sub, change_at, weighted(rng, CHURN_REASONS))
                    return
                is_upgrade = r < churn_p + upgrade_p
                self._cancel(sub, change_at, "upgrade" if is_upgrade else "downgrade")
                plan = plan + 1 if is_upgrade else plan - 1
                sub = self._new_subscription(user_id, plan, interval, change_at)
                self._order(user_id, sub[0], "upgrade" if is_upgrade else "new",
                            self._charge_amount(plan, interval), change_at)
                cursor = change_at
                continue

            if next_bill >= END:
                self._events(user_id, plan, sub[6], END)
                return

            status = "failed" if rng.random() < 0.04 else "paid"
            self._order(user_id, sub[0], "renewal", self._charge_amount(plan, interval), next_bill, status)
            if status == "failed":
                retry_at = next_bill + timedelta(days=2)
                if (END - next_bill).days < 14:
                    sub[3] = "past_due"
                    self._events(user_id, plan, sub[6], END)
                    return
                self._order(user_id, sub[0], "renewal", self._charge_amount(plan, interval), retry_at)
            cursor = next_bill

    def _events(self, user_id: int, plan: int, start: datetime, end: datetime, free_user: bool = False) -> None:
        rng = self.rng
        if free_user:
            # Free users are active for a few weeks, then mostly go quiet.
            end = min(end, start + timedelta(days=rng.expovariate(1 / 21)))
        days = (end - start).total_seconds() / 86400
        if days <= 0:
            return
        n = int(EVENTS_PER_DAY[plan] * days * rng.uniform(0.6, 1.4))
        platform_pref = weighted(rng, PLATFORMS)
        for _ in range(n):
            at = random_between(rng, start, end)
            if at.weekday() >= 5 and rng.random() < 0.45:
                continue
            platform = platform_pref if rng.random() < 0.8 else weighted(rng, PLATFORMS)
            self.events.append((len(self.events) + 1, user_id, weighted(rng, EVENT_NAMES), platform, at))


def copy_rows(cur: psycopg.Cursor, table: str, columns: str, rows) -> None:
    with cur.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def main() -> None:
    started = time.perf_counter()
    gen = Generator()
    gen.make_users()
    gen.simulate()
    gen.orders.sort(key=lambda o: o[6])
    gen.orders = [(i, *o[1:]) for i, o in enumerate(gen.orders, start=1)]
    gen.events.sort(key=lambda e: e[4])
    gen.events = [(i, *e[1:]) for i, e in enumerate(gen.events, start=1)]

    with psycopg.connect(ADMIN_URL) as conn, conn.cursor() as cur:
        cur.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        copy_rows(cur, "plans", "plan_id, name, tier_rank, monthly_price, annual_price, seat_limit", PLANS)
        copy_rows(cur, "users", "user_id, email, full_name, company_name, country, signup_source, created_at",
                  gen.users)
        copy_rows(cur, "subscriptions", "subscription_id, user_id, plan_id, status, billing_interval, mrr, "
                  "started_at, canceled_at, cancel_reason", gen.subscriptions)
        copy_rows(cur, "orders", "order_id, user_id, subscription_id, order_type, amount, status, created_at",
                  gen.orders)
        copy_rows(cur, "events", "event_id, user_id, event_name, platform, occurred_at", gen.events)
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro")
        cur.execute("ANALYZE")

    print(f"Seeded {len(gen.users):,} users, {len(gen.subscriptions):,} subscriptions, "
          f"{len(gen.orders):,} orders, {len(gen.events):,} events in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
