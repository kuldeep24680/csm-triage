# Solution Brief — Account Risk Triage for CSMs

**Author:** Kuldeep Chouhan. **Status:** written before the build; results are in the README.

## 1. The actual problem
- The VP asked for a health score. The underlying failure is different: **CSMs don't know who to call this week**. Each CSM has about 120 accounts and decides by guessing from a spreadsheet.
- The two lost renewals weren't a data problem. The signals existed in Zendesk and the product usage data. It was an **attention-allocation problem**: nobody looked at the right account early enough.
- So the product is a **ranked Monday call list**. For each account it answers four questions:
  - Who should I call?
  - Why?
  - What changed since last week?
  - What should I say?
- **Success metric:** precision@capacity. If a CSM can make about 15 meaningful calls a week, how many of their top 15 are truly at risk? Would the lost renewals have been flagged 30 days or more before renewal?

## 2. Where I disagree with the request
| Request | Concern | What I'll build instead |
|---|---|---|
| Auto-email accounts that go red | At-risk accounts need a human. An automated "we noticed you're unhappy" email can speed up churn. False positives spam healthy customers. An unvalidated model shouldn't speak to customers. | The agent **drafts** the outreach and queues it for the CSM, who approves with one click. Automate the noticing, not the talking. |
| Red / yellow / green score | A colour without a "why" or a "what changed" doesn't change behaviour. Fixed thresholds are arbitrary. | A calibrated churn probability, ranked by **risk × ARR × renewal proximity**. Top drivers and week-over-week change are shown. RYG bands are derived from probability and CSM capacity. |
| (implicit) every account gets a colour | Accounts with almost no data would look green, which means "fine". They are really unknown. | A distinct **"Insufficient signal"** state. Silence is a risk signal in its own right. |

## 3. What I'll build, in order
1. **Synthetic data with a known ground truth.**
   - Covers 3 tenant organisations, 5,000 accounts and about 1.2M records over 3 years across Salesforce, Gmail, Zendesk, product events and Stripe.
   - A hidden churn process generates the signals, so the model can be evaluated honestly.
   - Mess is deliberate: duplicates, late and out-of-order events, ID mismatches across sources, 3 years of schema drift, and sparse accounts.
2. **Ingestion and identity resolution.**
   - Idempotent loads that track both event time and ingest time.
   - Cross-source account matching, with every match decision logged.
3. **Point-in-time features and a fitted model.**
   - Predicts P(non-renewal within 90 days).
   - Logistic regression vs gradient boosting, trained on time-based splits, calibrated.
   - Benchmarked against a naive rules baseline.
   - Per-account drivers via feature attributions.
4. **Unattended agent run.**
   - A scheduled job ingests new data, rescores, detects changes, and builds each CSM's list.
   - An LLM writes the brief and draft email using only computed facts. Every claim must cite a feature or event; claims that can't be traced are dropped.
5. **CSM view.**
   - Shows the ranked list.
   - An account page drills down: score → drivers → features → raw source events, all tied to a `run_id` and `model_version`.

**Cross-cutting requirements:**
- **Multi-tenancy:** `tenant_id` on every row, isolation enforced in the database, and tenant-scoped API auth.
- **Traceability:** every number a CSM sees is reproducible from a stored scoring run.

## 4. What I need from the business
- **Churn definition:** non-renewal only, or downgrades and seat contraction too? What's the label window?
- **Historical outcomes** to use as labels, including the two lost renewals for backtesting.
- **Source of truth** for ARR and renewal date when Salesforce and Stripe disagree.
- **Identity key** across systems (email domain, Salesforce ID, Stripe customer ID).
- **CSM capacity** per week, and existing playbooks for each risk type.
- **Outreach constraints:** consent, approved templates, who is allowed to send.
- **Acceptable error trade-off:** is missing a churn worse than a wasted call? This sets the thresholds.

## 5. Deliberately out of scope (v1)
- Real connectors to Salesforce, Zendesk and the other sources (synthetic feeds use the same interface).
- Automated sending of email.
- Expansion and upsell scoring.
- Per-tenant custom models.
- SSO.
