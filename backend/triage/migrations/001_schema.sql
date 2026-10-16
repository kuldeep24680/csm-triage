-- Triage schema. Every tenant-owned table carries tenant_id and is protected by
-- row-level security (see 002_rls.sql). Global tables: tenants, users (login
-- lookup happens before a tenant is known), models.

CREATE TABLE tenants (
    tenant_id            text PRIMARY KEY,
    name                 text NOT NULL,
    timezone             text NOT NULL DEFAULT 'UTC',
    csm_weekly_capacity  int  NOT NULL DEFAULT 15,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    user_id        text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants,
    email          text NOT NULL UNIQUE,
    name           text NOT NULL,
    role           text NOT NULL CHECK (role IN ('csm', 'manager')),
    crm_user_id    text,            -- links app user to the CRM owner of accounts
    password_hash  text NOT NULL
);

-- ---------------------------------------------------------------- landing / raw
CREATE TABLE landing_files (
    tenant_id     text NOT NULL REFERENCES tenants,
    source        text NOT NULL,
    file_name     text NOT NULL,
    arrival_date  date NOT NULL,          -- when the file landed (drives point-in-time)
    records       int  NOT NULL,
    inserted      int  NOT NULL,
    duplicates    int  NOT NULL,
    rejected      int  NOT NULL DEFAULT 0,
    loaded_at     timestamptz NOT NULL DEFAULT now(),   -- wall clock of the load
    run_id        uuid,
    PRIMARY KEY (tenant_id, source, file_name)
);

CREATE TABLE raw_records (
    raw_id            bigserial PRIMARY KEY,
    tenant_id         text NOT NULL REFERENCES tenants,
    source            text NOT NULL,
    file_name         text NOT NULL,
    record_hash       text NOT NULL,      -- sha256 of canonical payload: exact-duplicate guard
    source_record_id  text,
    arrived_at        timestamptz NOT NULL,
    payload           jsonb NOT NULL,
    UNIQUE (tenant_id, source, record_hash)
);
CREATE INDEX ON raw_records (tenant_id, source, source_record_id);

CREATE TABLE rejected_records (
    tenant_id   text NOT NULL REFERENCES tenants,
    source      text NOT NULL,
    file_name   text NOT NULL,
    line_no     int,
    reason      text NOT NULL,
    payload     text,
    at          timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------- CRM (SFDC)
CREATE TABLE crm_users (
    tenant_id    text NOT NULL REFERENCES tenants,
    crm_user_id  text NOT NULL,
    name         text NOT NULL,
    email        text,
    PRIMARY KEY (tenant_id, crm_user_id)
);

-- Append-only versions so features can be reconstructed as of any date.
CREATE TABLE crm_account_versions (
    tenant_id       text NOT NULL REFERENCES tenants,
    account_id      text NOT NULL,       -- Salesforce Account Id = canonical account id
    name            text NOT NULL,
    website         text,
    domain          text,
    owner_id        text,
    segment         text,
    industry        text,
    arr             numeric(14,2),
    seats           int,
    contract_start  date,
    renewal_date    date,
    status          text,
    modified_at     timestamptz NOT NULL,
    arrived_at      timestamptz NOT NULL,
    raw_id          bigint,
    PRIMARY KEY (tenant_id, account_id, modified_at)
);

-- Latest state per account (rebuilt after each ingest). The book of business.
CREATE TABLE accounts (
    tenant_id       text NOT NULL REFERENCES tenants,
    account_id      text NOT NULL,
    name            text NOT NULL,
    domain          text,
    owner_id        text,
    segment         text,
    industry        text,
    arr             numeric(14,2),
    seats           int,
    contract_start  date,
    renewal_date    date,
    status          text NOT NULL,      -- active | churned
    PRIMARY KEY (tenant_id, account_id)
);

CREATE TABLE crm_contacts (
    tenant_id    text NOT NULL REFERENCES tenants,
    contact_id   text NOT NULL,
    account_id   text,
    name         text,
    email        text,
    title        text,
    is_champion  boolean NOT NULL DEFAULT false,
    modified_at  timestamptz NOT NULL,
    arrived_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, contact_id, modified_at)
);

CREATE TABLE crm_opportunities (
    tenant_id    text NOT NULL REFERENCES tenants,
    opp_id       text NOT NULL,
    account_id   text NOT NULL,
    opp_type     text,
    stage        text NOT NULL,
    close_date   date,
    amount       numeric(14,2),
    modified_at  timestamptz NOT NULL,
    arrived_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, opp_id, modified_at)
);

-- --------------------------------------------------------------- identity map
-- One row per external entity (Stripe customer, Zendesk org, product workspace,
-- email domain). Every decision is recorded with method, confidence, evidence.
CREATE TABLE identity_links (
    tenant_id    text NOT NULL REFERENCES tenants,
    source       text NOT NULL,
    external_id  text NOT NULL,
    account_id   text,                    -- NULL = unresolved (review queue)
    method       text NOT NULL,           -- explicit_id | domain | name_fuzzy | contact_email | unresolved
    confidence   real NOT NULL,
    evidence     jsonb NOT NULL,
    decided_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source, external_id)
);

-- ------------------------------------------------------------- support (Zendesk)
CREATE TABLE zendesk_orgs (
    tenant_id   text NOT NULL REFERENCES tenants,
    org_id      text NOT NULL,
    name        text,
    domains     text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, org_id)
);

CREATE TABLE tickets (
    tenant_id          text NOT NULL REFERENCES tenants,
    ticket_id          text NOT NULL,
    org_id             text,
    requester_email    text,
    subject            text,
    category           text,
    priority           text,             -- normalised: low | normal | high | urgent
    created_at         timestamptz NOT NULL,
    created_arrived_at timestamptz NOT NULL,
    solved_at          timestamptz,
    solved_arrived_at  timestamptz,
    csat               text,             -- good | bad | NULL
    PRIMARY KEY (tenant_id, ticket_id)
);

-- ------------------------------------------------------------------ email (Gmail)
CREATE TABLE emails (
    tenant_id     text NOT NULL REFERENCES tenants,
    message_id    text NOT NULL,
    thread_id     text,
    sent_at       timestamptz NOT NULL,
    direction     text NOT NULL,          -- inbound | outbound (relative to us)
    from_email    text NOT NULL,
    to_emails     text[] NOT NULL,
    counterpart   text,                   -- the customer-side address
    subject       text,
    snippet       text,
    arrived_at    timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, message_id)
);

-- ---------------------------------------------------------- product (telemetry)
CREATE TABLE product_workspaces (
    tenant_id       text NOT NULL REFERENCES tenants,
    workspace_id    text NOT NULL,
    workspace_name  text,
    account_hint    text,                 -- sf id when the v2 schema carries it
    PRIMARY KEY (tenant_id, workspace_id)
);

CREATE TABLE usage_weekly (
    tenant_id        text NOT NULL REFERENCES tenants,
    workspace_id     text NOT NULL,
    week_start       date NOT NULL,
    active_users     int,
    sessions         int,
    f_reports        int,
    f_integrations   int,
    f_automations    int,
    schema_version   int NOT NULL,
    arrived_at       timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, week_start)
);

-- ------------------------------------------------------------- billing (Stripe)
CREATE TABLE stripe_customers (
    tenant_id      text NOT NULL REFERENCES tenants,
    customer_id    text NOT NULL,
    name           text,
    email          text,
    account_hint   text,                  -- metadata.sf_account_id, when present
    PRIMARY KEY (tenant_id, customer_id)
);

CREATE TABLE billing_events (
    tenant_id       text NOT NULL REFERENCES tenants,
    event_id        text NOT NULL,
    event_type      text NOT NULL,
    customer_id     text NOT NULL,
    occurred_at     timestamptz NOT NULL,
    amount_cents    bigint,
    quantity        int,
    prev_quantity   int,
    arrived_at      timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);

-- ------------------------------------------------------------------- modelling
CREATE TABLE models (
    model_version   text PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    algorithm       text NOT NULL,
    feature_version text NOT NULL,
    features        jsonb NOT NULL,
    train_window    jsonb NOT NULL,
    metrics         jsonb NOT NULL,
    artifact        bytea NOT NULL,
    is_active       boolean NOT NULL DEFAULT false
);

CREATE TABLE feature_snapshots (
    tenant_id        text NOT NULL REFERENCES tenants,
    account_id       text NOT NULL,
    as_of            date NOT NULL,
    feature_version  text NOT NULL,
    features         jsonb NOT NULL,
    PRIMARY KEY (tenant_id, account_id, as_of, feature_version)
);

-- --------------------------------------------------------------------- agent
CREATE TABLE agent_runs (
    run_id         uuid PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants,
    as_of          date NOT NULL,
    trigger        text NOT NULL,        -- schedule | manual | backfill
    status         text NOT NULL,        -- running | succeeded | failed | partial
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    model_version  text,
    steps          jsonb NOT NULL DEFAULT '[]',
    stats          jsonb NOT NULL DEFAULT '{}',
    error          text
);
CREATE INDEX ON agent_runs (tenant_id, as_of DESC);

CREATE TABLE account_scores (
    tenant_id         text NOT NULL REFERENCES tenants,
    run_id            uuid NOT NULL REFERENCES agent_runs,
    account_id        text NOT NULL,
    as_of             date NOT NULL,
    owner_id          text,
    model_version     text NOT NULL,
    p_churn           real NOT NULL,
    priority          real NOT NULL,
    band              text NOT NULL,     -- call | watch | healthy | unknown
    rank_in_book      int,
    days_to_renewal   int,
    arr               numeric(14,2),
    insufficient_signal boolean NOT NULL,
    drivers           jsonb NOT NULL,
    changes           jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, account_id)
);
CREATE INDEX ON account_scores (tenant_id, account_id, as_of);

CREATE TABLE briefs (
    tenant_id       text NOT NULL REFERENCES tenants,
    run_id          uuid NOT NULL REFERENCES agent_runs,
    account_id      text NOT NULL,
    generator       text NOT NULL,
    summary         text NOT NULL,
    talking_points  jsonb NOT NULL,
    facts           jsonb NOT NULL,
    dropped_claims  jsonb NOT NULL DEFAULT '[]',
    meta            jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, account_id)
);

CREATE TABLE outreach_drafts (
    draft_id     uuid PRIMARY KEY,
    tenant_id    text NOT NULL REFERENCES tenants,
    run_id       uuid NOT NULL REFERENCES agent_runs,
    account_id   text NOT NULL,
    to_email     text,
    subject      text NOT NULL,
    body         text NOT NULL,
    status       text NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | superseded
    created_at   timestamptz NOT NULL DEFAULT now(),
    decided_by   text,
    decided_at   timestamptz,
    final_subject text,
    final_body    text
);
CREATE INDEX ON outreach_drafts (tenant_id, account_id, created_at DESC);

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    tenant_id  text NOT NULL REFERENCES tenants,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,
    action     text NOT NULL,
    entity     text,
    detail     jsonb NOT NULL DEFAULT '{}'
);
