-- Raw public-API responses cached per report, so resuming a report never re-fetches (M3).
CREATE TABLE IF NOT EXISTS api_cache (
    report_id    uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_hash text NOT NULL,
    source       text NOT NULL CHECK (source IN ('eurostat', 'worldbank')),
    url          text NOT NULL,
    payload      jsonb NOT NULL,
    retrieved_at timestamptz NOT NULL,
    PRIMARY KEY (report_id, request_hash)
);
