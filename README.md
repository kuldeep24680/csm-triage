# Triage

**Who to call this week, and why.**

Triage ranks each Customer Success Manager's (CSM's) accounts by churn risk, revenue at stake and renewal urgency. For each account it shows why it's on the list, what changed since last week, and what to say. A scheduled agent rescores every account each night and drafts outreach. **Nothing is emailed automatically**: a CSM approves every draft and sends it from their own mailbox. Every number on screen traces back to the run, the model version, the feature values and the raw source rows that produced it.

This README covers three things: **what I decided**, **what I deliberately skipped**, and **what breaks first at 10×**. The rest is reference material.

| Document | What's in it |
|---|---|
| [`BRIEF.md`](project_context/BRIEF.md) | Problem framing and why the original request was reshaped |
| [`DECISIONS.md`](project_context/DECISIONS.md) | Every design decision, with its reason and the alternative rejected |
| [`project_context/Triage_Solution_Brief.pptx`](project_context/Triage_Solution_Brief.pptx) | Solution brief deck |
| [`SETUP.md`](project_context/SETUP.md) · [`USER_FLOW.md`](project_context/USER_FLOW.md) | Run it locally · walk through it as a CSM |
| [`project_context/DEPLOYMENT.md`](project_context/DEPLOYMENT.md) | Production deployment (Docker Compose on one server) |

---

## Results (last 6 months, never seen in training)

Weekly replay of the call list across 3 companies, 38 weeks and 167 lost renewals. Every row uses the same capacity (15 calls per CSM per week) and the same revenue and renewal weighting; only the risk estimate changes.

| How the list is built | Churners listed 14–90 days before renewal | Lost revenue listed early | Hit rate (precision) |
|---|---|---|---|
| **Triage model** | **75%** | **95%** | **9.4%** |
| Red/yellow/green rules | 55% | 81% | 6.0% |
| Revenue + renewal date only (today) | 30% | 88% | 4.6% |
| Perfect foresight (ceiling) | 100% | 100% | 16.0% |

- **Ranking accuracy** (ROC AUC, where 0.5 is a coin flip): 0.75, against 0.73 for gradient boosting and 0.59 for the rules.
- **PR AUC:** 0.29 against a 9.8% base rate, about 3× better than random.
- **Calibrated:** a predicted "20%" means about 1 in 5 don't renew, within a few points per decile.
- **The two renewals from the brief:**
  - **Kestrel Health** (champion went silent) was listed 105 days before renewal; the rules never listed it.
  - **Brightline Logistics** (no product data, support storm) was listed from February; the rules waited until May.

---

## What I decided

The ten decisions that shape the system. All of them, with alternatives, are in [`DECISIONS.md`](project_context/DECISIONS.md).

| # | Decision | Why | Instead of |
|---|---|---|---|
| 1 | **The product is a ranked weekly call list per CSM**, with reasons and what changed. Colours are Red (call), Amber (watch), Green and **Grey (can't see)** | The failure was attention, not data. Red is set by CSM capacity (top 15), and missing data must never look healthy | A red/yellow/green health score |
| 2 | **No automatic customer email.** The agent drafts it, the CSM approves it and sends it from their own Gmail | The list's hit rate is ~10% per slot, so auto-send would mostly email healthy customers, and a "we noticed a problem" email can speed up churn | Auto-email when an account turns red |
| 3 | **A fitted model ranks; the LLM only explains.** Logistic regression trained on 29k+ point-in-time renewal snapshots. Scores are identical with the LLM switched off | Rankings must be measurable and reproducible. The model gives exact per-account reasons | LLM judgement, or gradient boosting (0.73 vs 0.75, and opaque) |
| 4 | **Sign-constrained model:** a signal whose weight points the wrong way (e.g. rising usage counted as risk) is dropped and the model refit | Collinearity was producing absurd explanations. Fixing it cost 0.007 accuracy and raised churners caught early from 71% to 75% | Shipping the highest accuracy number |
| 5 | **Priority is a policy, not part of the model:** risk × √revenue × renewal urgency, with **no minimum-risk cut-off** | √revenue stops a $500k account burying every $20k one. A cut-off dropped lost revenue caught early from 93% to 64% on validation | Thresholds on a colour |
| 6 | **Point-in-time everything:** every record keeps when it happened and when it arrived; a score only uses data that had arrived by its date | Late data is real (a 2-week telemetry outage backfilled a month later). Replays see exactly what the original run saw | Using whatever is in the table now |
| 7 | **Identity matching never guesses:** ID → email → domain → close name match; anything uncertain goes to an unresolved queue | Mixing two customers' data is worse than a gap. 2 wrong out of ~14k checked against the hidden truth | Best-guess fuzzy matching |
| 8 | **Isolation in the database:** `tenant_id` on every row plus forced Postgres row-level security; no tenant set returns zero rows | A forgotten `WHERE` clause can't leak another company's data | Filtering in application code |
| 9 | **LLM output is checked mechanically:** it sees only numbered facts; every point must cite one; every number must appear in the cited facts; the email can't mention risk; any failure falls back to a template | A made-up fact in a customer email is the worst failure | Trusting the prompt |
| 10 | **Nightly batch on one server:** Docker Compose (database, app, scheduler, HTTPS proxy). Each company runs at 02:00 in its own timezone, with one automatic retry that resumes from the failed step, plus a morning watchdog | The decision is weekly and churn builds over weeks (median 86-day lead). One box is enough at this scale and runs anywhere | Streaming / real-time, or managed cloud services |

---

## What I deliberately skipped

| Skipped | Why it's fine for v1 | What it would take |
|---|---|---|
| **Real connectors** (Salesforce, Zendesk, Gmail, Stripe APIs) | The synthetic feeds land in the same format a connector would write, so ingest, matching and features don't change | One connector per source writing JSONL by arrival date, plus OAuth, rate limits and backfill |
| **Sending email** | A deliberate product choice (decision 2) | An approved-sender integration, consent rules and templates the business signs off |
| **Real-time scoring** | Weekly decision; urgent events (outages, failed payments) already have owners | A same-day "interrupt" for critical events, layered on top of the nightly run, which is still needed to reconcile missed webhooks |
| **Per-company models** | The smallest company has ~100 renewals, too few to fit alone; one shared model with per-company calibration works | A hierarchical model once each company has enough history |
| **Downgrade / expansion prediction** | The label is "renewal lost"; whether downgrades count is a business question (see project_context/BRIEF.md §7) | A second label and model |
| **SSO, user management UI** | Seeded logins are enough for a demo; roles (CSM, manager) are enforced | OIDC/SAML login, invite flow |
| **A review screen for unmatched records** | The queue is shown on the Data quality page, not actionable | Approve/reject UI writing back to the identity links |
| **Migration framework** (Alembic) | Plain ordered SQL migrations; row-level security policies are SQL anyway | Switch when schema changes need rollbacks |
| **CI pipeline and high availability** | 24 automated tests plus a full end-to-end validation of the Docker stack were run before deploying; one server is acceptable for a demo | GitHub Actions (tests + image build per push); managed Postgres with backups and a second app instance |

---

## What breaks first at 10×

10× means 50,000 accounts and ~12M source records (or 30 companies). These are measured from today's runs, in the order they would fail.

| # | Breaks | Today | At 10× | Fix |
|---|---|---|---|---|
| 1 | **Memory in the nightly run.** Each run loads the company's full history into pandas to build features | Largest company (3,000 accounts): ~600 MB peak, 7 s to load | ~6 GB for one run, more for training: out of memory on the 8 GB server, and the run is killed | Keep daily per-account aggregates in Postgres, updated incrementally on ingest; features become window sums over those, and only accounts with new data are recomputed |
| 2 | **Briefs silently capped.** A cost guard writes LLM briefs for at most 400 call-list accounts per run | 300 call-list accounts in the largest company: under the cap | ~3,000 call-list accounts: ~2,600 Red accounts get no brief, with no error | Cap per CSM rather than per run, set a cost budget, and show "brief pending" explicitly. Brief reuse already skips ~64% of unchanged accounts |
| 3 | **Disk fills.** Scores and feature snapshots are kept for every run, with no retention | Database 1.4 GB; each nightly run adds ~10 MB | ~120 MB per night, ~45 GB a year, plus ~9 GB of raw records: the 30 GB disk fills in months | Partition score and snapshot tables by month; keep weekly snapshots long-term and prune daily ones; archive raw records to S3 |
| 4 | **Manager "all CSMs" call list.** Returns every scored account in the company in one response | 2,400 rows, ~150 ms | 24,000 rows with reasons: multi-second responses and a heavy page | Default to one CSM; paginate and aggregate server-side |
| 5 | **Identity matching time.** Close-name matching compares each unmatched record against every account name | ~1 s per run | ~100× more comparisons: minutes per run | Group by domain stem / first token before comparing, and only match records that are new since the last run |
| 6 | **One of everything.** One API process, one database, sequential nightly runs, sign-in throttling kept in memory | Fine for 3 companies | 30 companies in the same timezone queue behind each other; no failover; the throttle doesn't work across instances | Managed Postgres, 2+ API instances behind a load balancer with a shared throttle store, and a job queue so runs execute in parallel |

The first fix to make is #1: incremental features remove the memory limit and also make runs fast enough to consider same-day updates.

---

## How it meets the five requirements

| Requirement | How |
|---|---|
| **Messy synthetic data** | `triage/synth/`: ~1.2M records, 5 sources, 3 companies, 3 years. A hidden weekly health process plus shocks (champion leaves, product issues, budget cuts, competitor evaluation) drives every signal. Mess: duplicates, late and out-of-order arrival, 5 timestamp conventions, 3 schema changes, mismatched IDs, ~15% sparse accounts, stale CRM fields, corrupt lines |
| **A fitted model** | `triage/model/`: point-in-time features, time-based split, compared against gradient boosting and a rules score, per-company calibration, operational replay. The report is stored with the model and shown in the app |
| **Runs without a human** | The `scheduler` container runs `python -m triage run --trigger schedule` per company at 02:00 local time: ingest → match records → score → prioritise → diff → grounded briefs. Locked per company, idempotent, one automatic retry, morning watchdog, alerts |
| **Multi-tenant** | `tenant_id` everywhere plus forced Postgres row-level security (fails closed). Shared model, per-company calibration. Tests prove isolation |
| **Traceable** | Every score stores its run, model version and feature values. The evidence drawer shows the source rows behind any signal, including rows that arrived after the run (shown as unused). The API audit links a user name to a trace ID |

---

## Architecture

```
 landing zone (JSONL by arrival date)            Postgres (row-level security on every company table)
 ┌──────────────────────────────┐   ingest   ┌──────────────────────────────────────────┐
 │ salesforce zendesk gmail      │──────────▶ │ raw_records (sha256 dedup) → normalised  │
 │ product stripe   (per tenant) │  parse,    │ crm_*  tickets  emails  usage  billing   │
 └──────────────────────────────┘  tz, drift └───────────────┬──────────────────────────┘
                                                              │ identity_links (logged decisions)
                 nightly agent run (scheduler container)      ▼
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ features (point-in-time) → p(churn) → reasons → priority & bands per CSM  │
   │ → diff vs last run → facts → LLM brief + draft → validate citations       │
   │ → account_scores / feature_snapshots / briefs / outreach_drafts / runs    │
   └────────────────────────────────────────────────────────────────────────────┘
                                                              │
                     FastAPI (JWT → tenant → RLS)  ◀──────────┘   behind Caddy (HTTPS)
                     React: call list · account · evidence · runs · model · data quality
```

**Stack:** Python 3.12, FastAPI, pandas, scikit-learn, Postgres 16, OpenAI `gpt-4o-mini` (optional), React + TypeScript (Vite), Docker Compose, Caddy, OpenTelemetry.

```
backend/triage/
  synth/       generator.py (hidden process + mess), world.py
  ingest/      normalize.py (per-source parsing), loader.py (idempotent load), identity.py
  features/    build.py (point-in-time features, coverage, training snapshots)
  model/       train.py, evaluate.py (operational replay), calibration.py, explain.py, policy.py
  agent/       runner.py (the unattended run), grounding.py (facts + validation), llm.py
  api/         main.py, auth.py, audit.py
  scheduler.py nightly runs per company timezone + watchdog
  migrations/  001–005 (schema, row-level security, audit, run recovery, performance)
frontend/src/  main.tsx, pages.tsx, components.tsx, styles.css
deploy/docker/ compose.yml, Caddyfile, .env.example   (production)
deploy/ec2/    systemd alternative without Docker
```

---

## Run it

**Locally** (Python 3.12, Node 20.19+, Docker). Step-by-step: [`SETUP.md`](project_context/SETUP.md).

```bash
cp .env.example .env            # optional: add OPENAI_API_KEY
make setup && make db
make demo                       # generate → ingest → train → 8 weekly runs → logins (~8 min)
make api                        # http://localhost:8000   (terminal 1)
make web                        # http://localhost:5173   (terminal 2)
make test
```

Sign in as `manager@northwind.example` / `triage-demo` (also `helios`, `quill`). Every CSM has a login (`firstname.lastname@<company>.example`, same password) and sees only their own book.

**Production:** `cd deploy/docker && docker compose up -d --build`, then `docker compose exec api python -m triage bootstrap`. Full guide: [`project_context/DEPLOYMENT.md`](project_context/DEPLOYMENT.md).

**API docs:** Swagger at `/api/docs`. Every `/api` call is recorded with endpoint, status, latency, user and trace ID; managers can query `/api/audit/requests`.

Without an OpenAI key, briefs come from a template writer that uses the same facts and the same validation.
