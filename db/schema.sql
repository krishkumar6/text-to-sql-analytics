-- Demo SaaS schema. Recreated from scratch by scripts/seed.py.
-- Table and column comments are shown to the LLM, so keep them accurate.

DROP TABLE IF EXISTS events, orders, subscriptions, users, plans CASCADE;

CREATE TABLE plans (
    plan_id       integer        PRIMARY KEY,
    name          text           NOT NULL UNIQUE,
    tier_rank     integer        NOT NULL,
    monthly_price numeric(10, 2) NOT NULL,
    annual_price  numeric(10, 2) NOT NULL,
    seat_limit    integer
);

CREATE TABLE users (
    user_id       integer     PRIMARY KEY,
    email         text        NOT NULL UNIQUE,
    full_name     text        NOT NULL,
    company_name  text,
    country       text        NOT NULL,
    signup_source text        NOT NULL CHECK (signup_source IN ('organic', 'paid_search', 'social', 'referral', 'partner')),
    created_at    timestamp   NOT NULL
);

CREATE TABLE subscriptions (
    subscription_id  integer        PRIMARY KEY,
    user_id          integer        NOT NULL REFERENCES users (user_id),
    plan_id          integer        NOT NULL REFERENCES plans (plan_id),
    status           text           NOT NULL CHECK (status IN ('active', 'past_due', 'canceled')),
    billing_interval text           NOT NULL CHECK (billing_interval IN ('monthly', 'annual')),
    mrr              numeric(10, 2) NOT NULL,
    started_at       timestamp      NOT NULL,
    canceled_at      timestamp,
    cancel_reason    text CHECK (cancel_reason IN ('upgrade', 'downgrade', 'too_expensive', 'missing_features', 'switched_competitor', 'no_longer_needed'))
);

CREATE TABLE orders (
    order_id        integer        PRIMARY KEY,
    user_id         integer        NOT NULL REFERENCES users (user_id),
    subscription_id integer        REFERENCES subscriptions (subscription_id),
    order_type      text           NOT NULL CHECK (order_type IN ('new', 'renewal', 'upgrade', 'addon')),
    amount          numeric(10, 2) NOT NULL,
    status          text           NOT NULL CHECK (status IN ('paid', 'refunded', 'failed')),
    created_at      timestamp      NOT NULL
);

CREATE TABLE events (
    event_id    bigint    PRIMARY KEY,
    user_id     integer   NOT NULL REFERENCES users (user_id),
    event_name  text      NOT NULL,
    platform    text      NOT NULL CHECK (platform IN ('web', 'ios', 'android')),
    occurred_at timestamp NOT NULL
);

CREATE INDEX subscriptions_user_idx   ON subscriptions (user_id);
CREATE INDEX orders_user_idx          ON orders (user_id);
CREATE INDEX orders_created_at_idx    ON orders (created_at);
CREATE INDEX events_user_idx          ON events (user_id);
CREATE INDEX events_occurred_at_idx   ON events (occurred_at);
CREATE INDEX events_name_idx          ON events (event_name);

COMMENT ON TABLE plans IS 'Pricing plans. Free has price 0.';
COMMENT ON COLUMN plans.tier_rank IS '0 = Free, higher = more expensive tier. Use it to detect upgrades/downgrades.';
COMMENT ON COLUMN plans.annual_price IS 'Price per year when billed annually (two months free vs monthly).';
COMMENT ON COLUMN plans.seat_limit IS 'Max team seats; NULL = unlimited.';

COMMENT ON TABLE users IS 'One row per signed-up account.';
COMMENT ON COLUMN users.created_at IS 'Signup time (UTC).';
COMMENT ON COLUMN users.signup_source IS 'Acquisition channel at signup.';

COMMENT ON TABLE subscriptions IS 'Subscription periods. A plan change cancels the old row (cancel_reason upgrade/downgrade) and starts a new one, so a user can have several rows over time.';
COMMENT ON COLUMN subscriptions.status IS 'active or past_due = currently subscribed; canceled = ended at canceled_at.';
COMMENT ON COLUMN subscriptions.mrr IS 'Monthly recurring revenue of this subscription in USD (annual price / 12 for annual). 0 for Free.';
COMMENT ON COLUMN subscriptions.canceled_at IS 'NULL while the subscription is still running.';

COMMENT ON TABLE orders IS 'Payments/invoices charged to users.';
COMMENT ON COLUMN orders.order_type IS 'new = first payment of a subscription, renewal = recurring charge, upgrade = first charge after a plan upgrade, addon = extra seats/credits.';
COMMENT ON COLUMN orders.status IS 'Only paid counts as revenue.';

COMMENT ON TABLE events IS 'Product usage events, one row per action.';
COMMENT ON COLUMN events.event_name IS 'login, dashboard_viewed, project_created, report_exported, invite_sent, integration_connected, api_call.';
