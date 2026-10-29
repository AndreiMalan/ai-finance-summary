-- Exports (M9): one row per stored export file; a version groups the formats built together.
-- Also: approval time and the raw-file purge marker used by retention (D-009, AC 50), and the
-- indexes the daily cleanup scans by time.
ALTER TABLE reports ADD COLUMN IF NOT EXISTS approved_at timestamptz NULL;
-- Retention deletes a raw file's blob but keeps the row: observations reference it (provenance lives
-- for the report's lifetime) and ON DELETE CASCADE would remove them.
ALTER TABLE files ADD COLUMN IF NOT EXISTS purged_at timestamptz NULL;

CREATE TABLE IF NOT EXISTS report_versions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id   uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version     integer NOT NULL CHECK (version >= 1),
    fmt         text NOT NULL CHECK (fmt IN ('pdf', 'xlsx')),
    blob_key    text NOT NULL,
    size_bytes  bigint NOT NULL CHECK (size_bytes >= 0),
    sha256      text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, version, fmt)
);
CREATE INDEX IF NOT EXISTS report_versions_user ON report_versions (user_id);

CREATE INDEX IF NOT EXISTS reports_last_activity ON reports (last_activity_at);
CREATE INDEX IF NOT EXISTS files_unpurged ON files (report_id) WHERE purged_at IS NULL;
CREATE INDEX IF NOT EXISTS run_events_ts ON run_events (ts);
CREATE INDEX IF NOT EXISTS login_attempts_at ON login_attempts (at);
CREATE INDEX IF NOT EXISTS usage_events_at ON usage_events (at);
