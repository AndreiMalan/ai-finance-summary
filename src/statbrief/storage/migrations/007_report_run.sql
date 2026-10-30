-- Report runs (M7b): the parameters a run starts from, and a validated resume answer waiting for the next
-- run segment (POST /resume stores it; the segment that holds the report's run lock takes it exactly once).
ALTER TABLE reports ADD COLUMN IF NOT EXISTS params jsonb;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS pending_resume jsonb;
