-- One row per API request: who called what, how it went, how long it took.
-- Written off the request path by a background writer (see api/audit.py).
-- Request/response bodies are never stored; sensitive headers are redacted.

CREATE TABLE api_requests (
    id                bigserial PRIMARY KEY,
    request_id        uuid NOT NULL,
    at                timestamptz NOT NULL,
    tenant_id         text,                -- NULL for unauthenticated calls (login, health)
    user_id           text,
    user_email        text,
    role              text,
    method            text NOT NULL,
    path              text NOT NULL,       -- concrete path, e.g. /api/accounts/001abc
    route             text,                -- route template, e.g. /api/accounts/{account_id}
    query             text,
    status            int NOT NULL,
    latency_ms        real NOT NULL,
    response_bytes    bigint,
    client_ip         text,
    user_agent        text,
    request_headers   jsonb NOT NULL,
    response_headers  jsonb NOT NULL,
    error             text
);
CREATE INDEX ON api_requests (tenant_id, at DESC);
CREATE INDEX ON api_requests (route, at DESC);

ALTER TABLE api_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON api_requests TO triage_app
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY owner_all ON api_requests TO CURRENT_USER USING (true) WITH CHECK (true);
GRANT SELECT ON api_requests TO triage_app;
