-- Users, login attempts (throttling) and usage events (rate limits / daily LLM cap).
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    is_demo       boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id        bigserial PRIMARY KEY,
    email     text NOT NULL,
    ip        text NOT NULL,
    succeeded boolean NOT NULL,
    at        timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_email_ip_at ON login_attempts (email, ip, at);

CREATE TABLE IF NOT EXISTS usage_events (
    id      bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind    text NOT NULL,
    at      timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_events_kind_at ON usage_events (kind, at);
CREATE INDEX IF NOT EXISTS usage_events_user_kind_at ON usage_events (user_id, kind, at);
