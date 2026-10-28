-- Observability (M6, AC 51): one row per model call, tool call, retrieval or graph node of a report run.
-- `detail` never holds raw data values, prompt text or keys (unless LOG_PROMPTS debug mode is on).
CREATE TABLE IF NOT EXISTS run_events (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id         uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts                timestamptz NOT NULL DEFAULT now(),
    kind              text NOT NULL CHECK (kind IN ('llm_start', 'llm_end', 'llm_error', 'tool_start', 'tool_end',
                                                    'tool_error', 'retrieval', 'node')),
    name              text NULL,
    model             text NULL,
    latency_ms        integer NULL CHECK (latency_ms IS NULL OR latency_ms >= 0),
    prompt_tokens     integer NULL,
    completion_tokens integer NULL,
    detail            jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS run_events_report ON run_events (report_id, id);
