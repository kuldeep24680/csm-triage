# Decisions log

One line of context per decision. Most of these are things a reviewer will ask about.

## Product

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| P1 | Output is a ranked **weekly call list per CSM**, not a health score | The failure was attention, not data: CSMs didn't know who to call | A dashboard of RYG colours: shows state, doesn't drive action |
| P2 | **No automatic customer email.** The agent drafts; a CSM approves; the CSM sends from their own mailbox | An automated "we noticed you're unhappy" email can speed up churn; false positives spam healthy customers; the model's precision is ~10% per call slot | Auto-send on red: the VP's ask |
| P3 | Bands are derived from capacity, not fixed thresholds: `call` = top N by priority, `watch` = p ≥ 2× base rate, `unknown` = can't see | Thresholds on a colour are arbitrary; capacity is the real constraint | Red > 70 / yellow > 40 points |
| P4 | **"Can't see"** is its own band (no usage + no contact in 90 days) | Missing data must never render as green; this is how quiet accounts get lost | Impute and score anyway |
| P5 | Every account shows *why* (drivers), *what changed* (diff vs last run) and *what to say* (grounded brief) | A number without a reason doesn't change behaviour | Score only |

## Data

| # | Decision | Why |
|---|---|---|
| D1 | Landing files partitioned by **arrival date**; every normalised row keeps `arrived_at` | Late data is real (e.g. 2-week telemetry outage backfilled a month later). Lets any run be replayed exactly as it was |
| D2 | Three-level idempotency: file (landing_files PK), exact record (sha256 of canonical JSON), semantic key (message_id / ticket id / event id) | Re-running a load must change nothing; webhook retries and cross-mailbox duplicates differ in envelope |
| D3 | Bad lines go to `rejected_records` with the reason; the load continues | One corrupt line shouldn't stop a nightly run, and shouldn't vanish silently |
| D4 | Append-only CRM versions; tickets keep created/solved arrival separately | Needed for point-in-time reconstruction |
| D5 | CRM account id is the canonical identity | It's the book of business CSMs are measured on |
| D6 | Identity: explicit id → contact email → domain → domain stem → fuzzy name (≥92); otherwise **unresolved**, with the best candidate kept as evidence | A wrong match mixes two customers' data, which is worse than a missing match. Measured against the hidden truth: 2 wrong out of ~14k links |
| D7 | Free-mail domains never used for domain matching | gmail.com would match everyone |
| D8 | Identity links are resolved with current knowledge, and applied to history | A small, accepted leak: identity knowledge (not outcomes) from later events maps earlier ones. Documented, not hidden |
| D9 | Stale CRM renewal date → fall back to next contract anniversary, flagged in the UI | ~15% of renewals aren't rolled forward in the CRM promptly |

## Model

| # | Decision | Why | Evidence |
|---|---|---|---|
| M1 | Label = renewal opportunity Closed Lost. Downgrades are a feature, not the label | Clean, already in the CRM; open question for the business (in BRIEF) | |
| M2 | Snapshots 28/56/84/112 days before each renewal, point-in-time (event < as_of AND arrived ≤ as_of) | Train on what was knowable at decision time | |
| M3 | Time-based split: train → validate (12–6 months ago) → test (last 6 months, touched once) | Random splits leak the future | |
| M4 | **One pooled model + per-tenant Platt calibration** (when the tenant has ≥15 validation positives) | Quill has ~100 renewals: too few to fit alone. Calibration absorbs base-rate differences | Test AUC: Helios 0.79, Northwind 0.75, Quill 0.69 |
| M5 | Ship logistic regression over gradient boosting | Better on validation *and* exact per-account drivers. GBM only wins if it's clearly better (>0.03 PR-AUC) | Test PR-AUC 0.29 vs 0.26; ROC 0.75 vs 0.73 |
| M5b | **Sign-constrained** LR: features with a known direction (usage trend, tickets, champion silence…) are dropped if their coefficient points the wrong way, then refit | Collinearity gave rising usage a positive risk weight, so explanations read "usage +34%" as a risk reason. Dropped: wau_trend, automation_trend, payment_failures_90d | ROC 0.755 → 0.748, but churners caught early 71% → 75%, churned ARR 92% → 95% |
| M6 | Days-to-renewal is **not** a model feature | The model answers "will they leave?"; the policy answers "when to act". Avoids double counting | |
| M7 | Priority = p × √ARR × urgency(days to renewal) | √ARR so a $500k account doesn't bury every $20k account; urgency peaks 15–120 days out | |
| M8 | **No probability floor** on the call list; low-risk/high-value entries are labelled as such in the UI | Chosen on validation: a floor of 0.07 cut churned-ARR caught early from 93% to 64% (big accounts carry lower p but most revenue) | Validation sweep 0 / 0.07 / 0.10 / 0.13 |
| M9 | Judge the system on an **operational replay**, not just AUC | The decision is "who goes in 15 slots this week" | 75% of churners listed 14–90 days ahead vs 55% (rules) and 30% (ARR + date) |
| M10 | Report a perfect-foresight ceiling next to precision | 9% precision looks bad until you see the ceiling is 16% | |

## Agent & LLM

| # | Decision | Why |
|---|---|---|
| A1 | One run = one tenant × one date; per-tenant advisory lock; a failing tenant doesn't block others; non-zero exit on failure | Unattended runs need to be safe to retry and loud when they fail |
| A2 | Every score row stores run_id + model_version; every run stores its feature snapshot | Any number on screen is reproducible; the evidence drawer shows the exact rows, including rows that arrived *after* the run (hatched, unused) |
| A3 | LLM sees only numbered facts (F*) and events (E*); output must cite them; numbers must appear in cited facts; email can't contain internal risk language | Hallucinated facts in a customer email are the worst failure mode |
| A4 | Failed or unsupported LLM output falls back to a deterministic template, per field | A run never blocks on the LLM; works with no API key |
| A5 | Briefs only for the call list (cost cap `llm_max_accounts_per_run`) | ~200 accounts/night across tenants, not 4,000 |
| A6 | No new draft for an account approved in the last 14 days; older pending drafts are superseded | Don't make a CSM re-review the same outreach |

## Multi-tenancy

| # | Decision | Why |
|---|---|---|
| T1 | `tenant_id` on every row + Postgres **row-level security**, `FORCE`d, via `SET LOCAL ROLE triage_app` + `app.tenant_id` | Isolation enforced by the database even if a query forgets a filter. Unset tenant → zero rows (fails closed) |
| T2 | JWT carries the tenant; handlers can't choose it | |
| T3 | Model metadata is filtered per tenant too (the model card hides other tenants' metrics) | Metrics are data |

## Deliberately skipped (v1)

Real connectors, sending email, SSO, per-tenant models, expansion scoring, a human review UI for unresolved identities (the queue is listed, not actionable), streaming ingestion, Alembic (plain ordered SQL migrations instead: RLS policies are SQL anyway).
