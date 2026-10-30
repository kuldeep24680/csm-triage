# Code walkthrough

A module-by-module tour of the codebase: what each part does, how data flows through it, the rules it must never break, and **where to look when something goes wrong**. Read sections 1–2 first; after that, jump to the module you need.

---

## 1. The mental model

There are two independent flows that only meet in Postgres.

**A. The nightly run (writes).** Started by the scheduler, a manager's "Run now", or the CLI.

```
landing files ──► ingest ──► identity ──► load model ──► load data ──► score ──► save scores ──► briefs
 (data/landing)   loader.py   identity.py   runner.py      build.py      model/*   runner.py      grounding.py + llm.py
                  normalize.py                                                                    
                        │            │                                       │           │             │
                        ▼            ▼                                       ▼           ▼             ▼
                 raw_records   identity_links                         feature_snapshots  account_scores  briefs
                 + normalised tables                                                                     outreach_drafts
                                         agent_runs (status, steps, heartbeat, stats) ◄── written after every step
```

**B. A web request (reads, plus draft decisions).**

```
Browser ──► Caddy ──► audit middleware ──► FastAPI route ──► current_user (JWT) ──► tenant_tx(tenant) ──► SQL (row-level security) ──► JSON
                       api/audit.py          api/main.py      api/auth.py           db.py
```

**Three ideas run through the whole codebase:**
1. **Tenant isolation lives in the database.** Every query runs inside `tenant_tx(tenant_id)`, which switches to a restricted role and sets the company. Postgres then filters every row.
2. **Point-in-time.** Every source row keeps `arrived_at`. Features only use rows with `event_time < as_of AND arrived_at <= as_of`.
3. **Everything is traceable.** Each score row stores `run_id` and `model_version`; each run stores the exact feature values it used.

---

## 2. Repository map

```
backend/triage/
  config.py          settings (env vars, prefix TRIAGE_) + production safety checks
  db.py              connection pool, tenant_tx / system_tx, migrations
  cli.py             every command: generate, ingest, train, run, scheduler, bootstrap, serve…
  scheduler.py       long-running loop: nightly run per company + morning watchdog
  alerts.py          Slack-style webhook (or log line)
  observability.py   logging (JSON/text), OpenTelemetry traces + metrics, context vars
  synth/             synthetic data generator (world.py = vocabulary + company definitions)
  ingest/            normalize.py (per-source parsing), loader.py (load), identity.py (matching)
  features/build.py  point-in-time features, renewal dates, coverage, training snapshots
  model/             train.py, calibration.py, explain.py, policy.py, evaluate.py
  agent/             runner.py (the run), grounding.py (facts + validation), llm.py (writers)
  api/               main.py (routes), auth.py (login/JWT), audit.py (request audit middleware)
  migrations/        001 schema · 002 row-level security · 003 api_requests · 004 run recovery · 005 performance
backend/tests/       test_units.py (no DB) · test_db.py (needs loaded data)
frontend/src/        main.tsx (routing, login, shell) · pages.tsx · components.tsx · api.ts · styles.css
deploy/docker/       compose.yml, Caddyfile, .env.example   (production)
Dockerfile           web build + Python runtime (one image for api and scheduler)
```

---

## 3. Foundations: `config.py`, `db.py`, `cli.py`

### `config.py`

`Settings` reads env vars with the prefix `TRIAGE_` (plus `OPENAI_API_KEY`), and also reads the repo-root `.env` when running locally.

| Setting | Default | Why it matters |
|---|---|---|
| `database_url` | localhost triage/triage | Docker overrides it to host `db` |
| `sim_end_date` | 2026-10-16 | The "today" of the synthetic world |
| `use_wall_clock` | false | true → score the real date; false → `sim_end_date` |
| `llm_max_accounts_per_run` | 400 | Cost guard on briefs (a known 10× issue) |
| `llm_concurrency` | 16 | Parallel LLM calls |
| `reuse_briefs` | true | Fingerprint-based brief reuse |
| `retry_backoff_s` | 30 | Wait before the single automatic retry |
| `schedule_run_at` / `schedule_watchdog_at` | 02:00 / 06:30 | Company-local time / UTC |
| `env` | development | `production` enables `production_problems()` |

`production_problems()` refuses to start the API with a weak JWT secret or the default DB password.

### `db.py`: the most important 60 lines

- `pool()`: a psycopg connection pool (max 10), with the session timezone forced to UTC.
- `tenant_tx(tenant_id)`:
  1. validates the ID (`^[a-z0-9_-]{1,40}$`);
  2. opens a transaction;
  3. runs `SET LOCAL ROLE triage_app` (a role with no row-level-security bypass);
  4. runs `set_config('app.tenant_id', …, true)`.

  Every policy compares `tenant_id` with that setting. **If it isn't set, zero rows come back.**
- `system_tx()`: the owner role, with no tenant filter. Used only for migrations, login lookup, the tenant list, and model rows (models are shared).
- `migrate()`: applies `migrations/*.sql` in name order and records them in `schema_migrations`. Forward-only.

**Debug:** "a query returns nothing" almost always means it isn't inside `tenant_tx`, or it's running with the wrong tenant.

### `cli.py`

Every operation is a command: `python -m triage <cmd>`. The callback runs `observability.setup("triage-agent")`.

| Command | Does |
|---|---|
| `generate` | Writes the synthetic landing zone to `data/` |
| `ingest [--tenant] [--up-to]` | Loads landing files, then resolves identities |
| `train` | Builds training snapshots, fits, evaluates, registers the active model |
| `run [--tenant] [--as-of] [--trigger] [--resume RUN_ID] [--no-retry]` | One nightly run; exits 1 on failure |
| `backfill --weeks 8` | Replays weekly runs, so "what changed" has history |
| `watchdog` | Abandons stuck runs and starts missing ones |
| `scheduler [--dry-run]` | The long-running scheduler; `--dry-run` prints the next fire times |
| `bootstrap` | First-time setup, idempotent (migrate → generate → ingest → train → backfill → users) |
| `seed-users`, `migrate`, `reset-db --yes`, `serve`, `alert "msg"` | As named |

The date used everywhere comes from `_d()`: `--as-of`, else today (if `use_wall_clock`), else `sim_end_date`.

---

## 4. Synthetic data: `synth/`

**Purpose:** produce what 5 real systems would drop into a landing zone, with realistic mess, driven by a hidden process so outcomes can be learned.

- `world.py`:
  - **`TENANTS`**: Northwind (3,000 accounts, 20 CSMs, New York), Helios (1,500, 10, London), Quill (500, 4, Kolkata, higher churn bias, capacity 12).
  - Name vocabularies.
- `generator.py`:
  - `Gen.simulate(account)` walks each account week by week. A hidden **health** value drifts and takes **shocks**: champion leaves, product issue, budget cut, competitor, expansion. Observable signals (usage, tickets, emails, billing) are noisy, lagged reflections of health. Renewal outcome = f(health, shocks, noise).
  - `plant_stories()` scripts the four named accounts: Brightline, Kestrel, Orchid Freight, Summit Dental.
  - `Out.add(source, day, payload, lag=…)` writes each record into the file for its **arrival** day, so lateness is built in.
  - Mess (see the module docstring): duplicates, a telemetry outage backfilled a month later, 5 timestamp formats, 3 schema changes, mismatched IDs, ~15% sparse accounts, stale CRM, corrupt lines.
- **Output:**
  - `data/landing/<tenant>/<source>/<arrival-date>.jsonl` plus `_tenant.json`.
  - `data/truth/<tenant>.jsonl`, the hidden truth, **never read by the pipeline**; used only to measure identity accuracy.

**Debug:** it's deterministic (seed 7). The same seed and end date give the same data, which is why the server and local logins match.

---

## 5. Ingest: `ingest/`

### `normalize.py`: one parser per source

- `PARSERS = {salesforce, zendesk, gmail, product, stripe}`. Each takes `(record, ctx)` and returns `[(table, row), …]`.
- Handles drift:
  - Salesforce `ACV__c → ARR__c`.
  - Zendesk numeric → string priority (`ZD_PRIORITY`).
  - Product v1 (naive local time, `wau`) → v2 (UTC, `active_users`).
- Timezones:
  - Gmail RFC 2822 offsets → UTC.
  - Product v1 is interpreted in the company's timezone (`ctx["tz"]`).
  - Stripe epoch seconds.
- `norm_domain` / `registrable` / `email_domain` normalise domains. `FREE_MAIL` is the set never used for matching.
- `Reject` means a missing required field; the caller logs it as a rejected line.

### `loader.py`: landing files → database

- `register_tenants()` reads `data/landing/*/_tenant.json` and upserts `tenants`. **The scheduler and runs depend on the data volume being mounted.**
- `pending_files()` lists files not yet in `landing_files` with arrival ≤ `up_to`.
- `load_chunk()`:
  1. Parses each line; a failure becomes a `rejected_records` row with a reason.
  2. Inserts into **`raw_records`** with the sha256 of the canonical JSON (`ON CONFLICT DO NOTHING`), which handles exact duplicates.
  3. Upserts the normalised tables on their natural keys, which handles semantic duplicates. The earliest arrival wins (`LEAST(...)` for ticket arrival times).
  4. Records the per-file counts in `landing_files`.
- `ingest_tenant()` processes files in 45-day chunks, each in its own `tenant_tx`.

**Invariants:**
- Loading the same file twice changes nothing.
- `arrived_at` is the file's arrival date, not the event time.

### `identity.py`: link every external record to a CRM account

- `rebuild_accounts()` refreshes `accounts` from the latest CRM versions.
- `Resolver.resolve()` tries, in order:

| Order | Method | Confidence |
|---|---|---|
| 1 | `explicit_id` (CRM ID carried in the record) | 1.0 |
| 2 | `contact_email` | 0.98 |
| 3 | `domain` (must be unique; free mail skipped) | 0.95 |
| 4 | `domain_stem` | 0.9 |
| 5 | `name_fuzzy` ≥ 92 | the match score |
| — | 80–91, an ambiguous domain, or nothing | `unresolved`, with evidence |

- `resolve_identities()` resolves Stripe customers, Zendesk orgs, product workspaces and every customer email address, then upserts `identity_links`. It only updates a row when the account or method changed.

**Debug:**
- "A ticket isn't showing for an account": check `identity_links` for that org and requester email.
- Tickets attach through the org link first, then the requester email (`COALESCE(org, email)`).

---

## 6. Features: `features/build.py`

- `FEATURES`: the 24 signals with human labels. `FEATURE_VERSION = "fv1"` (bump it when a definition changes).
- `load_frames(conn, tenant)` loads the company's **whole history** into pandas, already joined to account IDs through `identity_links`. **This is the 10× memory bottleneck.**
- `accounts_as_of(fr, as_of)`:
  - The CRM state as it was on that date: the latest version that had *arrived*.
  - `active` is false if churn is known from any source (CRM status, a Closed Lost opportunity, or a Stripe cancellation).
  - The renewal date comes from the CRM, or, if missing or stale, from `next_anniversary(contract_start)`. `renewal_source` says which.
- `snapshot(fr, as_of, accounts)` builds one row per account, every source filtered with `_visible()` (event < T, arrived ≤ T):
  - **Usage:** complete weeks only; 4-week vs 8-week log ratios (`_log_ratio`); `weeks_since_usage` capped at 52.
  - **Support:** 30/90-day counts; urgent and high priority; open tickets; bad CSAT only once the solve had arrived; data-export requests.
  - **Email:** inbound counts and trend; days since last inbound; days since the *champion's* last email; negative-language hits (`NEGATIVE_LEXICON`).
  - **Billing:** failed payments in 90 days; largest seat contraction in 180 days.
  - **Account:** log ARR, tenure, segment flags.
- `coverage(row)`: `insufficient` = fewer than 3 usage weeks in 12 **and** no contact in 90 days. That's the Grey band.
- `urgency(days)`: 0.8 overdue to 14 days · 1.0 for 15–120 · 0.6 to 180 · 0.3 to 270 · 0.1 beyond that · 0.3 unknown.
- `training_snapshots()` / `build_matrix()`: labelled rows at 28/56/84/112 days before each past renewal. Label = Closed Lost.

**Debug:**
- A feature value looks wrong → open the evidence drawer in the UI. It runs the same windows in SQL and shows rows that arrived after the run.
- Also compare against `feature_snapshots.features` for that run date.

---

## 7. Model: `model/`

### `train.py`: `train_and_register(as_of)`

1. `load_dataset()`: training snapshots for every company, pooled.
2. **Time split by renewal date:**

   | Set | Renewals |
   |---|---|
   | train | before `as_of − 365d` |
   | validate | `−365d` to `−182d` |
   | test | the last 182 days |

3. `fit_sign_constrained_lr()`: a pipeline of median imputer (with missing flags) → scaler → LR (C = 0.3). Any feature in `EXPECTED_SIGN` whose coefficient has the wrong sign is dropped and the model refit.
4. Also fits `make_gbm()` (gradient boosting).
5. Calibrates each on **validation**: `fit_calibrators()`, Platt, a global calibrator plus one per company that has ≥ 15 positives.
6. **Choose on validation:** GBM only if its PR AUC beats LR by more than 0.03.
7. `watch_min_p = 2 × validation base rate` (the Amber threshold).
8. Scores the test set **once**; computes per-company / per-lead-time / per-coverage metrics and the reliability table.
9. `operational_eval()` (see below).
10. Pickles a `ChurnModel` into `models.artifact` with the full report in `models.metrics`, and sets it active.

`ChurnModel.predict(X, tenant)` = the company's calibrator (or the global one) applied to the raw probability.

### `calibration.py`

`PlattCalibrator`: `p = sigmoid(a·logit(raw) + b)`, fit on validation.

### `explain.py`

`contributions()`: for LR, the standardised value × coefficient, with the missing-value indicators folded back into their base feature. `top_drivers()` returns the top risk and protective reasons with display strings (`describe_value`). These are the "Why" bars.

### `policy.py`

- `priority = p × √max(ARR, 1) × urgency(days)`.
- `assign_bands(book, capacity, watch_min_p)`, per CSM book:

| Band | Rule |
|---|---|
| unknown (Grey) | insufficient signal |
| call (Red) | top *capacity* by priority, no probability floor |
| watch (Amber) | p ≥ `watch_min_p` |
| healthy (Green) | the rest |

### `evaluate.py`: `operational_eval()`

Replays every week of the test period. For each strategy (model, rules, revenue + date only, oracle) it builds each CSM's top-*capacity* list with the same priority formula, then checks whether lost renewals were listed 14–90 days before they happened. This produces the 75% / 55% / 30% / 100% table and the precision vs ceiling.

**Debug:**
- Model metrics are in the `models.metrics` JSON (and on the Model page).
- To retrain, run `python -m triage train`. The next run uses the new active model; a resumed run keeps its original model version.

---

## 8. The nightly run: `agent/runner.py`

### Lifecycle

- `run_all(as_of, tenant, trigger, auto_retry)`:
  - Loops over companies, calling `run_tenant()`.
  - If a run fails: sleeps `retry_backoff_s`, then **resumes once**.
  - Still failed or partial → `alerts.send`.
- `run_tenant()`:
  1. Takes `pg_try_advisory_lock(hashtext('triage:<tenant>'))`. If it isn't free, returns `skipped`.
  2. While holding the lock, `_abandon_dead_runs()` marks any leftover `running` row as `abandoned`, since its process must be dead, and alerts.
  3. Either creates an `agent_runs` row (`running`), or, on resume, reloads the old row's steps, increments `attempts` and pins `model_version`.
  4. Calls `_execute(run)` inside an `agent.run` trace span.
  5. Saves `succeeded`, `partial` (if any brief failed) or `failed`, with the traceback.
- `Run.step(name, fn, skippable)`:
  - Times the step, records it in `run.steps` and calls `beat()`, which writes `heartbeat_at`, `steps` and `stats`.
  - On resume, skippable steps that already finished (`ingest`, `identity`, `persist_scores`) are skipped.

### `_execute(run)`, step by step

| Step | What happens |
|---|---|
| `ingest` | `ingest_tenant(t, as_of, run_id)` |
| `identity` | `resolve_identities()` |
| `load_model` | `load_active_model(run.model_version)` (cached per version) |
| `load_data` | `load_frames()` + ticket subjects, CSM names, the **previous successful run's** scores and features (for the diff) |
| `score` | Active accounts → `snapshot` → `model.predict` → `top_drivers` → `coverage` → `priority` → `assign_bands` per CSM |
| (diff) | `compute_changes(prev, prev_feats, cur, feats)`: band moves, risk ±5 points, and the `CHANGE_RULES` (new urgent tickets, seat cut, champion silent 30+ days…) |
| `persist_scores` | Writes `feature_snapshots` and `account_scores` (with `run_id`, `model_version`, drivers, changes) |
| `reload_scores` | Only on resume when the scores were already saved |
| `briefs` | See below |

### The briefs step

1. **Pick accounts:** Red accounts, top `llm_max_accounts_per_run` by priority.
2. **Slice the data:** `_account_views()` cuts the frames once, for just these accounts. This was the 23s → 5s fix.
3. **Build facts and fingerprint:** for each account, `_facts()` calls `grounding.build_facts`, then `grounding.fingerprint` hashes them, ignoring things that drift daily (score, days-away, change wording).
4. **Try reuse:** `_reuse()` looks for a brief from the last 14 days with the same fingerprint, then re-validates it against today's facts. If anything gets dropped, it writes a new one.
5. **Write a new brief** (`_brief`):
   - `writer.write` (OpenAI or template) → `grounding.validate`.
   - Any field that failed is backfilled from the template.
   - The primary contact (champion first) becomes the `to` address.
6. **Save in batches of 50** (`_persist_batch`):
   - Writes the briefs (with `facts_hash` and `email`).
   - Creates drafts, except when the account was approved in the last 14 days, or the brief was reused and a pending or identical draft already exists.
   - Older pending drafts are superseded.
7. **Concurrency:** a thread pool of `llm_concurrency`; `contextvars.copy_context()` carries the trace, tenant and run IDs into the threads.

### Other entry points

- `resume_run(run_id)`: resumes a failed or abandoned run (used by the API and `--resume`).
- `watchdog(as_of)`:
  - Marks runs with no heartbeat for 15 minutes as `abandoned`, and alerts.
  - If a company has no successful run for `as_of`, it alerts and starts one (`trigger=watchdog`).

**Debug:**
- Agent runs page → **Steps**: the failing step, its error and timings.
- `SELECT status, attempts, steps, error FROM agent_runs ORDER BY started_at DESC LIMIT 5;`
- `skipped` means the lock is held: another run is in progress.
- `abandoned` means the process died (OOM, container restart). Check the container logs and memory.

---

## 9. LLM and grounding: `agent/grounding.py`, `agent/llm.py`

- `build_facts(acct, score, drivers, changes, events)` returns a numbered list:

| ID | Content |
|---|---|
| F1 | Account (ARR, renewal date) |
| F2 | Score and rank |
| F3… | Reasons (`driver`) and protective factors |
| Then | What changed |
| E1… | Recent events (tickets, emails, failed payments, seat cuts) |

  Each fact carries a `ref` back to its table.
- `validate(output, facts)`:
  - Drops talking points with no valid citation, or with numbers absent from the cited facts (`_numbers_ok`; ISO date parts are allowed).
  - Blanks the summary if it has unsupported numbers.
  - Rejects the email if it contains `INTERNAL_TERMS` (churn, risk score, flagged…) or unsupported numbers.
  - Returns `(clean_output, dropped[])`; the dropped list is stored as `briefs.dropped_claims`.
- `fingerprint(facts, csm, generator)`: a sha256 of the stable facts plus the *kinds* of changes. It ignores the score fact and day counts.
- `llm.py`:
  - `SYSTEM_PROMPT` gives the rules: cite IDs, use only numbers from the facts, no risk talk in the email, refer to dates rather than day counts, and sign with the CSM's first name.
  - `OpenAIWriter` (gpt-4o-mini, JSON mode, with an `llm.brief` span and token metrics).
  - `TemplateWriter` (deterministic, `template-v2`).
  - `get_writer()` returns OpenAI when a key is set, else the template.

**Debug:**
- A brief looks thin → check its `dropped_claims` (the claims removed and why), and `meta.error` if the LLM call failed.
- The generator name (e.g. `openai:gpt-4o-mini+template`) shows which fields fell back.

---

## 10. Scheduler and alerts: `scheduler.py`, `alerts.py`

- `plan(now, tenants)`:
  - One `run:<tenant>` job at `schedule_run_at` in **each company's timezone**, read from the `tenants` table on every loop.
  - Plus the `watchdog` job at `schedule_watchdog_at` UTC.
- `loop()`:
  - Keeps a `since` window, so a job that falls due while another job is running still fires next loop.
  - Jobs run one at a time, each as a **child process** (`python -m triage run --tenant X --trigger schedule`) with a 2-hour timeout.
- `execute()`: exit code 0 = ok; 1 = the run failed (the run already alerted); anything else (killed, OOM, timeout) → the scheduler alerts.
- `alerts.send(title, details)`: POSTs `{"text": …}` to `TRIAGE_ALERT_WEBHOOK_URL`, or logs at ERROR if no webhook is set.

**Debug:**
- `docker compose exec scheduler python -m triage scheduler --dry-run` shows the next fire times.
- `docker compose logs scheduler` shows `job start` / `job end rc=…`.

---

## 11. API: `api/main.py`, `api/auth.py`, `api/audit.py`

### Auth (`auth.py`)

- Passwords: pbkdf2 (`hash_password` / `verify_password`).
- `login()`:
  - Throttles: 10 failures per email or 50 per IP within 15 minutes → 429.
  - Looks the user up with `system_tx` (the tenant isn't known yet).
  - Issues an HS256 JWT with `sub`, `tenant`, `role`, `crm` (CRM user ID), `name`, `email`, `exp` (12 h).
- `current_user()`: decodes the token → `User`. **The tenant always comes from the token, never from the request.**
- `seed_users()`: creates one manager per company plus one login per CRM user (CSM). Idempotent.

### Routes (`main.py`)

| Method · path | Role | What it does |
|---|---|---|
| `GET /api/health` | public | DB ok, active model, last run and last success per company, stuck runs |
| `GET /api/auth/config` | public | Whether to hint the demo password (never in production) |
| `POST /api/auth/login` | public | → `{token, user}` |
| `GET /api/me` | any | User, company and (managers only) the CSM list |
| `GET /api/calllist?owner=&run_id=` | any | Latest run's scores. CSMs are forced to their own book (`_owner_scope`). Returns tallies, Red/Amber/Grey lists, 12-week history and draft status |
| `GET /api/team` | manager | Per-CSM counts and drafts approved/pending |
| `GET /api/accounts/{id}` | owner CSM or manager | Score, features, history (52 weeks), latest brief, latest draft, contacts, identity links, 120-day timeline |
| `GET /api/accounts/{id}/evidence?feature=` | same | Raw rows behind one signal, with `visible_to_run` (false = arrived late) |
| `POST /api/drafts/{id}/approve` · `/reject` | owner CSM or manager | Only `pending` drafts (409 otherwise); stores the final subject/body and writes `audit_log` |
| `GET /api/runs` | any | Last 50 runs, with `stale` (heartbeat older than 15 minutes) |
| `POST /api/runs` | manager | Starts a run as a **child process** (`_spawn`). 409 if one is running |
| `POST /api/runs/{id}/resume` | manager | Only failed or abandoned runs; 404 / 409 rules |
| `GET /api/model` | any | Model card, filtered to the user's company |
| `GET /api/data-quality` | any | Load counts, duplicates, late data, identity methods, unresolved queue, rejected lines |
| `GET /api/audit/requests?user_name=&trace_id=&status=&route=&errors_only=` | manager | The API audit log |
| `GET /api/audit/summary?hours=&user_name=` | manager | Rate, errors and latency per route |
| `GET /{path}` | public | Serves the React app (SPA fallback); unknown `/api/*` paths → 404 |

Key helpers:
- `_latest_run()`: the latest **succeeded or partial** run.
- `_linked()` + `_TICKETS_FOR_ACCOUNT`: fast per-account source lookups using the account's linked IDs (migration 005 indexes).
- `_require_manager`, `_ensure_idle` and `_spawn` back the run controls.

### Audit (`audit.py`)

`audit_middleware` runs on every `/api` request. It:
1. reads identity from the bearer token;
2. sets context vars (request ID, tenant, user name) used by the logs;
3. tags the OpenTelemetry span;
4. times the request and adds the `X-Request-ID`, `X-Trace-ID` and `X-Response-Time-ms` headers;
5. records metrics;
6. queues an audit row. Headers are redacted, and the Authorization header is reduced to a fingerprint.

A background thread batch-inserts the queued rows into `api_requests`; `/api/health` is skipped. It never blocks a request, and a full queue drops records with a warning.

**Debug — "user X says it broke at 10:05":**
1. `GET /api/audit/requests?user_name=X` → the row (status, latency, error) and its `trace_id`.
2. `docker compose logs api | grep <trace_id>` → every log line for that request.

---

## 12. Observability: `observability.py`

- `setup(service)`:
  - Logging: JSON when `TRIAGE_LOG_FORMAT=json`. Every line carries `trace_id`, `request_id`, `tenant`, `user_name`, `run_id`.
  - OpenTelemetry: tracer and meter providers; OTLP export **only** if `OTEL_EXPORTER_OTLP_ENDPOINT` is set; psycopg instrumentation (a span per DB query).
- `M` / `record()`: counters and histograms — `api_requests`, `api_latency`, `runs`, `run_duration`, `step_duration`, `llm_tokens`, `briefs`, `claims_removed`.
- Context vars (`request_id_var`, `tenant_var`, `user_name_var`, `run_id_var`) are copied into worker threads.

---

## 13. Database: `migrations/`

| Group | Tables |
|---|---|
| Companies and users | `tenants`, `users` |
| Raw and ingest bookkeeping | `landing_files`, `raw_records` (sha256), `rejected_records` |
| CRM (append-only versions) | `crm_users`, `crm_account_versions`, `accounts` (latest view), `crm_contacts`, `crm_opportunities` |
| Other sources | `zendesk_orgs`, `tickets`, `emails`, `product_workspaces`, `usage_weekly`, `stripe_customers`, `billing_events` |
| Identity | `identity_links` (source, external_id → account_id, method, confidence, evidence) |
| Decisions | `models`, `agent_runs`, `feature_snapshots`, `account_scores`, `briefs`, `outreach_drafts` |
| Audit | `audit_log` (user actions), `api_requests` (every API call) |

The migrations in order:
- **002** enables and **forces** row-level security on every company table, with a `tenant_isolation` policy (`tenant_id = current_setting('app.tenant_id', true)`) for `triage_app`, plus `owner_all` for the owner role.
- **004** adds `heartbeat_at`, `attempts` and `last_resumed_at` to `agent_runs`, and `user_name` and `trace_id` to `api_requests`.
- **005** adds the account-page indexes and the brief-reuse columns.

---

## 14. Frontend: `frontend/src/`

- `api.ts`:
  - `api(path)` wraps `fetch('/api' + path)` with the bearer token from `localStorage` (`triage.token`).
  - On a 401 it clears the token and goes to `/login`.
  - Formatters live here too.
- `main.tsx`:
  - Routes: `/login`, `/`, `/team`, `/accounts/:id`, `/runs`, `/model`, `/data`.
  - `RequireAuth`; `Shell` loads `/me` into `MeCtx`.
  - The navigation is role-aware: managers get Team.
- `pages.tsx`:

| Page | What it shows |
|---|---|
| `CallListPage` | Tally, legend, Red/Amber/Grey sections, `AccountRow` |
| `AccountPage` | Why, What changed, Prep for the call, `DraftPanel` (approve/reject, Open in Gmail), timeline, how the score was made |
| `TeamPage` | Per-CSM summary |
| `RunsPage` | Runs table, Steps, Run now / Resume (managers), polls every 4s while a run is going |
| `ModelPage` | Replay, metrics, calibration chart, lost renewals, coefficients |
| `DataQualityPage` | Ingest and identity statistics |

- `components.tsx`: `Sparkline`, `BandLegend`, `Band`, `Drivers` (clicking one opens `EvidenceDrawer`), `EvidenceDrawer` (rows with a late-arrival hatch).
- **Dev:** Vite on :5173 proxies `/api` to :8000. **Prod:** the API serves `frontend/dist`.

---

## 15. Debugging playbook

| Symptom | Where to look | Likely cause / fix |
|---|---|---|
| Login 401 | `users` table; is the password the one from `bootstrap`? | Wrong password, or `seed-users` never ran |
| Login 429 | `auth._failures` (in memory) | Throttled; wait 15 minutes or `docker compose restart api` |
| API won't start: "refusing to start in production" | `.env` | Weak `TRIAGE_JWT_SECRET` or default DB password |
| Empty call list | `GET /api/health` → `last_success` | No successful run yet → `python -m triage run --tenant X` |
| Account missing from list | `account_scores` for the latest run; `accounts_as_of` active flag | Churned, contract not started, or wrong owner |
| Ticket/email not on account | `identity_links` for its org/email | Unresolved or matched to another account (Data quality page) |
| Run `skipped` | `pg_locks` | Another run holds the company lock |
| Run `abandoned` | `docker compose logs scheduler`, `free -h` | Process killed (memory or restart); Resume it |
| Run `failed` at `briefs` | `agent_runs.steps[].error`; `briefs.meta.error` | OpenAI key/quota; the run resumes once automatically |
| Brief missing for a Red account | `llm_max_accounts_per_run` | Over the 400 cap (10× issue) |
| "Run now" gives 409 | Running process / `agent_runs` | A run is already in progress |
| Slow page | `/api/audit/summary` (latency by route) | Missing migration 005? `python -m triage migrate` |
| HTTPS fails | `docker compose logs caddy` | Ports 80/443 closed, or `SITE_HOST` doesn't resolve to the server |

### Handy SQL

Open a shell with `docker compose exec db psql -U triage triage`. The owner role sees every company, so filter by `tenant_id`.

```sql
-- latest runs
SELECT tenant_id, as_of, trigger, status, attempts, started_at, error FROM agent_runs ORDER BY started_at DESC LIMIT 10;
-- one account's score in the latest northwind run
SELECT p_churn, band, rank_in_book, drivers->'risk', changes FROM account_scores
 WHERE run_id = (SELECT run_id FROM agent_runs WHERE tenant_id='northwind' AND status IN ('succeeded','partial') ORDER BY started_at DESC LIMIT 1)
   AND account_id = '<id>';
-- how a source record was matched
SELECT * FROM identity_links WHERE tenant_id='northwind' AND external_id = '<org id / email / customer id>';
-- what the brief was allowed to say, and what was removed
SELECT generator, facts, dropped_claims, meta FROM briefs WHERE account_id='<id>' ORDER BY run_id DESC LIMIT 1;
-- unresolved identities
SELECT source, count(*) FROM identity_links WHERE tenant_id='northwind' AND method='unresolved' GROUP BY 1;
-- slowest routes in the last hour
SELECT route, count(*), round(avg(latency_ms)) FROM api_requests WHERE at > now() - interval '1 hour' GROUP BY 1 ORDER BY 3 DESC;
```

### Running pieces in isolation (local)

```bash
cd backend
python -m triage run --tenant quill --no-retry           # one run, no retry, full JSON result
python -m triage watchdog --no-start                     # report only
python -m triage scheduler --dry-run
python -m pytest -q                                      # 24 tests (DB tests need loaded data)
```

---

## 16. Trying the API: Swagger first, Postman optional

**Swagger is built in** at `/api/docs` (ReDoc at `/api/redoc`). It lists every route with its parameters and example bodies, so there's no separate collection to maintain.

1. Open `https://<host>/api/docs` (locally: `http://localhost:8000/api/docs`).
2. **POST /api/auth/login** → *Try it out* → enter an email and password → *Execute* → copy the `token`.
3. Click **Authorize** (top right), paste the token, then **Authorize**.
4. Call any route: `/api/calllist`, `/api/accounts/{id}`, `/api/runs`…

**Postman:** you don't need a hand-made collection. In Postman: *Import* → *Link* → `https://<host>/api/openapi.json`. It creates the collection from the live schema, so it can't drift from the code. Then set a collection-level Bearer token.

**curl:**

```bash
TOKEN=$(curl -s -X POST https://<host>/api/auth/login -H 'content-type: application/json' \
  -d '{"email":"manager@northwind.example","password":"<password>"}' | jq -r .token)
curl -s https://<host>/api/calllist -H "Authorization: Bearer $TOKEN" | jq '.counts'
curl -sI https://<host>/api/health | grep -i x-trace-id
```
