-- Run recovery: liveness + resume bookkeeping.
ALTER TABLE agent_runs ADD COLUMN heartbeat_at timestamptz;
ALTER TABLE agent_runs ADD COLUMN attempts int NOT NULL DEFAULT 1;
ALTER TABLE agent_runs ADD COLUMN last_resumed_at timestamptz;
-- status now also takes 'abandoned' (process died without finishing)

-- Find a user's requests (and their traces) when they report a problem.
ALTER TABLE api_requests ADD COLUMN user_name text;
ALTER TABLE api_requests ADD COLUMN trace_id text;
CREATE INDEX ON api_requests (tenant_id, user_name, at DESC);
CREATE INDEX ON api_requests (trace_id);
