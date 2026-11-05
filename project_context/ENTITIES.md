# Entities (Postgres tables)

Every table the app uses, what it's for, and what each row holds. Schema: `backend/triage/migrations/001–006`.

- **Tenant isolation:** every company table has `tenant_id` + forced row-level security. Exceptions: `models` and `schema_migrations` (shared); `refresh_tokens` (owner role only).
- **History:** nothing in the source and decision layers is overwritten. New versions and new runs add rows.

## Summary

| # | Entity | Layer | Purpose | Written by | Read by |
|---|---|---|---|---|---|
| 1 | `tenants` | Company | One row per company: timezone, CSM call capacity | ingest (`register_tenants`) | everything |
| 2 | `users` | Company | App logins (CSM or manager), password hash | `seed-users` / `bootstrap` | login |
| 3 | `refresh_tokens` | Company | Sign-in sessions: hash of each refresh token, rotation chain | login / refresh / logout | refresh |
| 4 | `landing_files` | Raw | Which landing files were loaded, with counts (file-level dedup) | ingest | ingest, Data quality |
| 5 | `raw_records` | Raw | Every source record verbatim + sha256 (exact-copy dedup) | ingest | debugging, replay |
| 6 | `rejected_records` | Raw | Lines that failed to parse, with the reason | ingest | Data quality |
| 7 | `crm_users` | Normalised | CRM users = CSMs who own accounts | ingest | call list, team, seed-users |
| 8 | `crm_account_versions` | Normalised | Every version of each CRM account (point-in-time) | ingest | features, identity |
| 9 | `accounts` | Normalised | Latest view of each account (master list) | identity (`rebuild_accounts`) | identity, API |
| 10 | `crm_contacts` | Normalised | Customer contacts, versioned; champion flag | ingest | features, identity, drafts |
| 11 | `crm_opportunities` | Normalised | Renewal opportunities, versioned; **Closed Lost = the label** | ingest | training, evaluation |
| 12 | `zendesk_orgs` | Normalised | Support organisations + domains | ingest | identity |
| 13 | `tickets` | Normalised | Support tickets (merged create + solve) | ingest | features, timeline, evidence |
| 14 | `emails` | Normalised | Gmail messages with the customer | ingest | features, timeline, evidence |
| 15 | `product_workspaces` | Normalised | Product workspaces (+ CRM hint in schema v2) | ingest | identity |
| 16 | `usage_weekly` | Normalised | Weekly product usage per workspace | ingest | features, evidence |
| 17 | `stripe_customers` | Normalised | Billing customers (+ CRM hint) | ingest | identity |
| 18 | `billing_events` | Normalised | Payments, failures, seat changes, cancellations | ingest | features, evidence |
| 19 | `identity_links` | Identity | External ID → CRM account, with method, confidence, evidence | identity | features, API, Data quality |
| 20 | `models` | Decisions | Trained model versions: pickled artifact + metrics; one active | `train` | runs, Model page |
| 21 | `agent_runs` | Decisions | One row per nightly run: status, steps, heartbeat, stats | runner | Runs page, watchdog, health |
| 22 | `feature_snapshots` | Decisions | The 24 feature values per account per date | runner | diff, evidence, traceability |
| 23 | `account_scores` | Decisions | Per run per account: p, priority, band, rank, reasons, changes | runner | call list, account page |
| 24 | `briefs` | Decisions | Grounded call-prep brief: facts, output, what was dropped | runner (`briefs` step) | account page, reuse |
| 25 | `outreach_drafts` | Decisions | Email draft for the CSM to approve or reject | runner; API (decide) | account page, team |
| 26 | `audit_log` | Audit | User actions: draft approve/reject, Run now, Resume | API | managers, debugging |
| 27 | `api_requests` | Audit | Every `/api` call: user, route, status, latency, trace ID | audit middleware | `/api/audit/*` |
| 28 | `schema_migrations` | System | Which migration files have been applied | `migrate` | `migrate` |

**Flow:** 4–6 (raw) → 7–18 (normalised) → 19 (identity) → 22–25 (decisions, made with 20) → read by the API; 26–27 audit it all.

---

## Company and access

### `tenants`
- **Key:** `tenant_id`
- **Stores:** `name`, `timezone` (drives 02:00 local runs), `csm_weekly_capacity` (15; Quill 12), `created_at`.

### `users`
- **Key:** `user_id`; `email` unique.
- **Stores:** `tenant_id`, `name`, `role` (`csm` | `manager`), `crm_user_id` (links a CSM to the accounts they own), `password_hash` (pbkdf2).

### `refresh_tokens`
- **Key:** `token_hash` (sha256; the raw token exists only in the user's cookie).
- **Stores:** `family_id` (one sign-in), `user_id`, `tenant_id`, `created_at`, `expires_at` (12 h), `revoked_at`, `replaced_by` (next token in the rotation).
- **Rule:** reuse of a revoked token → the whole family is revoked.

## Raw layer

### `landing_files`
- **Key:** `(tenant_id, source, file_name)`; a file is never loaded twice.
- **Stores:** `arrival_date`, `records`, `inserted`, `duplicates`, `rejected`, `loaded_at`, `run_id`.

### `raw_records`
- **Key:** `raw_id`; unique `(tenant_id, source, record_hash)`.
- **Stores:** `payload` (original JSON), `record_hash` (sha256 of the canonical JSON), `source_record_id`, `file_name`, `arrived_at`.
- **Why:** audit trail; any parsing bug can be replayed from here.

### `rejected_records`
- **Stores:** `source`, `file_name`, `line_no`, `reason`, `payload` (raw text), `at`.

## Normalised layer (one per source)

Semantic keys below are the **source's own IDs**: they catch the same event re-sent in a different shape. The earliest `arrived_at` wins.

| Table | Key | Main columns | On re-send |
|---|---|---|---|
| `crm_users` | `crm_user_id` | `name`, `email` | update |
| `crm_account_versions` | `account_id, modified_at` | `name`, `domain`, `owner_id`, `segment`, `industry`, `arr`, `seats`, `contract_start`, `renewal_date`, `status`, `arrived_at`, `raw_id` | new version |
| `accounts` | `account_id` | latest of the above; `status` = active \| churned | rebuilt each run |
| `crm_contacts` | `contact_id, modified_at` | `account_id`, `name`, `email`, `title`, `is_champion`, `arrived_at` | new version |
| `crm_opportunities` | `opp_id, modified_at` | `account_id`, `opp_type`, `stage`, `close_date`, `amount`, `arrived_at` | new version |
| `zendesk_orgs` | `org_id` | `name`, `domains[]` | update |
| `tickets` | `ticket_id` | `org_id`, `requester_email`, `subject`, `category`, `priority` (low/normal/high/urgent), `created_at` + `created_arrived_at`, `solved_at` + `solved_arrived_at`, `csat` (good/bad) | merge |
| `emails` | `message_id` | `thread_id`, `sent_at`, `direction` (inbound/outbound), `from_email`, `to_emails[]`, `counterpart` (customer address), `subject`, `snippet`, `arrived_at` | first wins |
| `product_workspaces` | `workspace_id` | `workspace_name`, `account_hint` (CRM ID, v2 only) | update |
| `usage_weekly` | `workspace_id, week_start` | `active_users`, `sessions`, `f_reports`, `f_integrations`, `f_automations`, `schema_version`, `arrived_at` | first wins |
| `stripe_customers` | `customer_id` | `name`, `email`, `account_hint` (CRM ID from metadata) | update |
| `billing_events` | `event_id` | `event_type` (payment, failure, seat change, cancel), `customer_id`, `occurred_at`, `amount_cents`, `quantity`, `prev_quantity`, `arrived_at` | first wins |

(All keys are prefixed with `tenant_id`.)

## Identity

### `identity_links`
- **Key:** `(tenant_id, source, external_id)`, e.g. (`zendesk`, org ID) or (`email`, address).
- **Stores:** `account_id` (NULL = unresolved), `method` (`explicit_id` | `contact_email` | `domain` | `domain_stem` | `name_fuzzy` | `unresolved`), `confidence` (0–1), `evidence` (JSON: why, best candidate, score), `decided_at`.
- **Updated:** only when the account or method changes.

## Decisions

### `models`
- **Key:** `model_version` (`churn-log-YYYYMMDD-HHMMSS`). Shared across companies.
- **Stores:** `algorithm`, `feature_version` (`fv1`), `features` (list used), `train_window` (split dates), `metrics` (full report: AUC, PR-AUC, calibration, replay, per company), `artifact` (pickled model + calibrators), `is_active`.

### `agent_runs`
- **Key:** `run_id`.
- **Stores:** `tenant_id`, `as_of`, `trigger` (`schedule` | `manual` | `backfill` | `watchdog`), `status` (`running` | `succeeded` | `partial` | `failed` | `abandoned` | `skipped`), `started_at`, `finished_at`, `heartbeat_at`, `attempts`, `last_resumed_at`, `model_version` (pinned on resume), `steps` (per-step timing/status/error), `stats` (counts), `error` (traceback).

### `feature_snapshots`
- **Key:** `(tenant_id, account_id, as_of, feature_version)`.
- **Stores:** `features`: the 24 values exactly as the model saw them.

### `account_scores`
- **Key:** `(run_id, account_id)`.
- **Stores:** `as_of`, `owner_id`, `model_version`, `p_churn`, `priority`, `band` (`call` | `watch` | `healthy` | `unknown`), `rank_in_book`, `days_to_renewal`, `arr`, `insufficient_signal`, `drivers` (top risk and protective reasons with contributions), `changes` (what changed since the last run).

### `briefs`
- **Key:** `(run_id, account_id)`.
- **Stores:** `generator` (e.g. `openai:gpt-4o-mini+template`), `summary`, `talking_points` (each with fact citations), `email` (drafted subject/body), `facts` (the F/E list it was allowed to use), `dropped_claims` (removed by the validator, with reasons), `facts_hash` (fingerprint for reuse), `meta` (tokens, errors, `reused_from_run`).

### `outreach_drafts`
- **Key:** `draft_id`.
- **Stores:** `run_id`, `account_id`, `to_email`, `subject`, `body`, `status` (`pending` | `approved` | `rejected` | `superseded`), `created_at`, `decided_by`, `decided_at`, `final_subject`, `final_body` (what the CSM approved).

## Audit

### `audit_log`
- **Key:** `id`.
- **Stores:** `tenant_id`, `at`, `actor`, `action` (`draft.approved` | `draft.rejected` | `run.start` | `run.resume`), `entity`, `detail` (JSON).
- **Written** in the same transaction as the action, so it's never lost.

### `api_requests`
- **Key:** `id`.
- **Stores:** `request_id`, `trace_id`, `at`, `tenant_id`, `user_id`, `user_email`, `user_name`, `role`, `method`, `path`, `route` (template), `query`, `status`, `latency_ms`, `response_bytes`, `client_ip`, `user_agent`, `request_headers` / `response_headers` (redacted; token reduced to a fingerprint), `error`.
- **Written** asynchronously (queue + batch insert); `/api/health` is skipped.

## System

### `schema_migrations`
- **Key:** `name` (migration file).
- **Stores:** `applied_at`. Migrations are forward-only and applied in name order on API start.
