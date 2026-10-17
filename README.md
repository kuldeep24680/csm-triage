# Triage

**Who to call this week, and why.**

Triage ranks each Customer Success Manager's accounts by churn risk, revenue at stake and renewal urgency, then tells them why each account is on the list, what changed since last week, and what to say. A scheduled agent rescores every account, detects changes and drafts outreach. **Nothing is emailed automatically**: a CSM approves every draft and sends it from their own mailbox. Every number on screen traces back to the run, model version, feature values and raw source rows that produced it.

- Problem framing and pushback on the original request: [`BRIEF.md`](BRIEF.md)
- Every design decision with its reason: [`DECISIONS.md`](DECISIONS.md)

---

## Results (held-out last 6 months, never seen in training)

Weekly replay of the call list across 3 tenants, 38 weeks, 167 lost renewals. Same capacity and the same revenue/renewal weighting in every row; only the risk estimate changes.

| How the list is built | Churners listed 14–90 days before renewal | Churned ARR listed early | Precision of the list |
|---|---|---|---|
| **Triage model** | **75%** | **95%** | **9.4%** |
| Red/yellow/green rules | 55% | 81% | 6.0% |
| ARR + renewal date only (today) | 30% | 88% | 4.6% |
| Perfect foresight (ceiling) | 100% | 100% | 16.0% |

Ranking quality (renewal snapshots): logistic regression ROC AUC **0.75**, PR AUC **0.29** at a 9.8% base rate (3× lift). Gradient boosting 0.73 / 0.26. Rules 0.59 / 0.13. Calibrated: predicted and observed churn rates match within a few points per decile.

The two renewals from the brief: **Kestrel Health** (champion went silent, no tickets) was listed 105 days before renewal; the rules never listed it. **Brightline Logistics** (no product telemetry, support storm) was listed from February; the rules waited until May.

---

## How it meets the five requirements

| Requirement | Where |
|---|---|
| **Messy synthetic data** | `triage/synth/`: ~1.2M records, 5 sources, 3 tenants, 3 years. A hidden weekly health process plus shocks (champion leaves, product issues, budget cuts, competitor evaluation) drives every signal. Mess: exact and semantic duplicates, late and out-of-order arrival (incl. a 2-week telemetry outage backfilled a month later), 5 timestamp conventions, 3 schema changes, mismatched identities, ~15% sparse accounts, stale CRM fields, corrupt lines |
| **A fitted model** | `triage/model/`: point-in-time features, time-based split, LR vs GBM vs rules, per-tenant calibration, operational replay. Report stored with the model and shown in the app |
| **Runs without a human** | `python -m triage run --trigger schedule`: ingest → resolve identities → score → prioritise → diff → grounded briefs → record. Advisory-locked, idempotent, per-tenant failure isolation, non-zero exit on failure |
| **Multi-tenant** | `tenant_id` everywhere + Postgres row-level security (forced; fails closed). Pooled model, per-tenant calibration. Tests prove isolation |
| **Traceable** | Every score: run_id, model_version, stored feature snapshot. The evidence drawer shows the source rows behind any feature, including rows that arrived after the run (hatched, unused) |

---

## Architecture

```
 landing zone (JSONL by arrival date)            Postgres (RLS on every tenant table)
 ┌──────────────────────────────┐   ingest   ┌──────────────────────────────────────────┐
 │ salesforce zendesk gmail      │──────────▶ │ raw_records (sha256 dedup) → normalised  │
 │ product stripe   (per tenant) │  parse,    │ crm_*  tickets  emails  usage  billing   │
 └──────────────────────────────┘  tz, drift └───────────────┬──────────────────────────┘
                                                              │ identity_links (logged decisions)
                    nightly / weekly agent run                ▼
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ features (point-in-time) → p(churn) → drivers → priority & bands per CSM  │
   │ → diff vs last run → facts → LLM brief + draft → validate citations       │
   │ → account_scores / feature_snapshots / briefs / outreach_drafts / runs    │
   └────────────────────────────────────────────────────────────────────────────┘
                                                              │
                     FastAPI (JWT → tenant → RLS)  ◀──────────┘
                     React: call list · account · evidence · runs · model · data quality
```

```
backend/triage/
  synth/       generator.py (hidden process + mess), world.py (vocabulary, tenants)
  ingest/      normalize.py (per-source parsing), loader.py (idempotent load), identity.py
  features/    build.py (point-in-time features, coverage, training snapshots)
  model/       train.py, evaluate.py (operational replay), calibration.py, explain.py, policy.py
  agent/       runner.py (the unattended run), grounding.py (facts + validation), llm.py
  api/         main.py, auth.py
  migrations/  001_schema.sql, 002_rls.sql
frontend/src/  main.tsx, pages.tsx, components.tsx, styles.css
```

---

## Run it locally

Needs Python 3.12, Node 20.19+ (or 22.12+), Docker.

```bash
cp .env.example .env            # add OPENAI_API_KEY to use the LLM; optional
make setup                      # venv + npm install
make db                         # Postgres 16 in Docker
make demo                       # reset → generate → ingest → train → 8 weekly runs → users (~8 min)
make api                        # http://localhost:8000  (terminal 1)
make web                        # http://localhost:5173  (terminal 2)
make test
```

Sign in with `manager@northwind.example` / `triage-demo` (also `helios`, `quill`). Every CSM in the CRM also has a login (`firstname.lastname@<tenant>.example`, same password), which sees only their own book.

What to look at:
1. **Call lists**: pick a CSM. Rank, risk, reasons, what changed, draft status.
2. Open an account: **Why** (click any signal to see its source rows), **What changed**, **Prep for the call** (hover the citation chips), **Draft email** (approve or reject; nothing is sent).
3. Search Northwind for *Orchid Freight* (champion left, competitor evaluation, sparse telemetry) and *Summit Dental Group* (budget cut + support issue): live at-risk accounts.
4. **Agent runs**: each run's steps, timings, counts, claims removed. **Model**: the evaluation above. **Data quality**: duplicates, late data, identity matching, review queue.

API docs: Swagger UI at http://localhost:8000/api/docs (ReDoc at `/api/redoc`). Every `/api` call is recorded in `api_requests` (endpoint, route, redacted headers, status, latency, caller); managers can query it at `/api/audit/requests` and `/api/audit/summary`.

CLI (`cd backend && python -m triage --help`): `generate`, `ingest`, `train`, `run`, `backfill`, `seed-users`, `serve`, `migrate`, `reset-db`.

Without an OpenAI key, briefs come from a deterministic template writer: same facts, same validation. With a key, `gpt-4o-mini` writes them and the validator strips anything it can't support.

---

## What breaks at 10× (and what I'd do)

| Breaks first | Why | Fix |
|---|---|---|
| Feature computation | Loads a tenant's history into pandas each run (~16s for Northwind today) | Incremental daily aggregates in Postgres (or a warehouse), keyed by account and day; features become window sums |
| Ingest | Single process, Python JSON parsing (~13k rec/s) | Parallelise per tenant/source; COPY straight from object storage; partitioned tables by month |
| LLM cost/latency | One call per call-list account | Only regenerate briefs when facts change; batch API; cache by facts hash |
| Fuzzy identity matching | O(n·m) against all account names | Blocking by domain stem / first token; human review queue as a real workflow |
| One pooled model | Tenants with very different products drift apart | Tenant features, hierarchical model, per-tenant drift monitoring and retrain triggers |
| Tables | account_scores grows by accounts × runs | Partition by month; keep weekly snapshots, prune daily ones |

## Not built (on purpose)

Real connectors (the synthetic feeds use the same landing interface), sending email, SSO, per-tenant models, expansion scoring, an actionable identity review UI. Deployment and observability are next.
