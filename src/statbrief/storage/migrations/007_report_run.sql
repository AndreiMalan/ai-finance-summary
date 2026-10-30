-- Report runs (M7b): the parameters a run starts from, a validated resume answer waiting for the next run
-- segment (POST /resume stores it together with the interrupt id it answers; the segment that holds the
-- run lease applies it only if that interrupt is still pending), and the run lease itself: a row-level
-- lease (token + expiry) instead of a session advisory lock, so it works through Neon's transaction-mode
-- pooler and expires by itself when an instance is frozen or killed (R5).
ALTER TABLE reports ADD COLUMN IF NOT EXISTS params jsonb;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS pending_resume jsonb;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS run_lease_token uuid;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS run_lease_until timestamptz;
