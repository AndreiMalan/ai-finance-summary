-- Reports, uploaded files (blob refs), chunked uploads, per-file mapping/validation, normalized observations.
CREATE TABLE IF NOT EXISTS reports (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            text NOT NULL,
    status           text NOT NULL DEFAULT 'draft',
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_activity_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reports_user ON reports (user_id, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS files (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id   uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('csv', 'xlsx', 'xls', 'json')),
    size_bytes  bigint NOT NULL,
    sha256      text NOT NULL,
    blob_key    text NOT NULL,
    status      text NOT NULL DEFAULT 'uploaded',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS files_report ON files (report_id);

CREATE TABLE IF NOT EXISTS uploads (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id    uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    filename     text NOT NULL,
    total_bytes  bigint NOT NULL,
    chunk_count  int NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS upload_chunks (
    upload_id uuid NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    idx       int NOT NULL,
    data      bytea NOT NULL,
    PRIMARY KEY (upload_id, idx)
);

CREATE TABLE IF NOT EXISTS file_mappings (
    file_id    uuid PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    mapping    jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS validation_reports (
    file_id    uuid PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    report     jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS observations (
    report_id      uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    obs_id         text NOT NULL,
    file_id        uuid REFERENCES files(id) ON DELETE CASCADE,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    indicator_code text NOT NULL,
    region_code    text NOT NULL,
    period         text NOT NULL,
    payload        jsonb NOT NULL,
    PRIMARY KEY (report_id, obs_id)
);
CREATE INDEX IF NOT EXISTS observations_key ON observations (report_id, indicator_code, region_code, period);
-- replace_for_file deletes by file; without this index that is a sequential scan of all observations.
CREATE INDEX IF NOT EXISTS observations_file ON observations (file_id);
