# Triage: Solution Brief

**Who to call this week, and why.**
Kuldeep Chouhan · October 2026 · Code: this repo · Decisions: [DECISIONS.md](DECISIONS.md) · Run it: [SETUP.md](SETUP.md)

---

## 1. At a glance

- **Problem:** CSMs have ~120 accounts each and no reliable way to pick who to call. Two renewals were lost while the warning signs sat in Zendesk and product data.
- **What Triage does:** every week it gives each CSM a ranked call list, shows **why** each account is on it and **what changed**, and drafts outreach for the CSM to approve.
- **How it decides:** a model fitted on 3 years of history scores every account. The LLM only writes notes for accounts the model has already ranked.
- **Result on 6 months of data the model never saw:** catches **75% of churners early** (vs 55% for a red/yellow/green rules score), covering **95% of churned revenue**.
- **Safety:** nothing is emailed automatically, every number traces back to its source rows, and each company's data is isolated by the database.

---

## 2. The request, and what we built instead

The VP asked for *"a health score per account, red / yellow / green, and if an account goes red, email them automatically."*

| VP asked for | What Triage does | Why |
|---|---|---|
| Health score per account | Calibrated **chance of not renewing**, with the reasons behind it | A colour alone doesn't say what to do |
| Red / yellow / green | **Red** call this week · **Amber** watch · **Green** healthy · **Grey** can't see | Red is set by CSM capacity and real risk, not an arbitrary cut-off. No data shows as grey, never green |
| Auto-email when red | Email is **auto-drafted**; the CSM edits, approves and sends from their own Gmail | An automated "we noticed a problem" email can speed up churn, and a wrong flag spams a happy customer |
| "Nothing slips" | Weekly ranked list, a "can't see" section, and a team view of unreviewed drafts | Every account is covered without spamming anyone |

---

## 3. How it works

```
5 sources ──▶ ingest ──▶ match records ──▶ 24 signals ──▶ model ──▶ rank & colour ──▶ brief + draft ──▶ CSM
(CRM, Zendesk,  (dedupe,     to accounts      (point in     (churn %)   (per CSM,        (LLM, checked     (approves,
 Gmail, product, late data,                    time)                     capacity 15)      against facts)    sends)
 Stripe)         timezones)
```

1. **Data:** synthetic but realistic. 3 companies, 5,000 accounts, ~1.2M records over 3 years, with deliberate mess: duplicates, late and out-of-order data, timezone and schema changes, mismatched IDs, sparse accounts, stale CRM fields.
2. **Ingest:** loads each file once, drops duplicates, rejects corrupt lines with a reason, and converts all times to UTC.
3. **Match records to accounts:** links Stripe, Zendesk, product and email records to CRM accounts. Uncertain matches go to a review queue instead of being guessed (2 wrong out of ~14,000 when checked against the hidden truth).
4. **Signals:** 24 per account (usage trend, seat use, tickets, CSAT, champion silence, negative emails, seat cuts…), using only data that had arrived by the scoring date.
5. **Model:** logistic regression trained on past renewal outcomes, calibrated per company, and compared against gradient boosting and a rules score.
6. **Rank & colour:** priority = risk × √revenue × renewal urgency. The top 15 per CSM are Red.
7. **Brief & draft:** the LLM writes call notes and an email using only numbered facts. A validator removes anything unsupported or any internal risk language. Works without an LLM via a template.
8. **Runs unattended:** a scheduled job does all of the above for each company, with locking, retries and per-company failure isolation.

---

## 4. Results (held-out last 6 months)

| How the call list is built | Churners listed 14–90 days early | Churned revenue listed early | List precision |
|---|---|---|---|
| **Triage model** | **75%** | **95%** | **9.4%** |
| Red/yellow/green rules | 55% | 81% | 6.0% |
| Revenue + renewal date only (today) | 30% | 88% | 4.6% |
| Perfect foresight (ceiling) | 100% | 100% | 16% |

- **Ranking quality:** ROC AUC 0.75 vs 0.59 for rules. The percentages are calibrated: "20%" ≈ 1 in 5 don't renew.
- **The two lost renewals:**
  - **Kestrel Health** (champion went silent): flagged 105 days before renewal. The rules never flagged it.
  - **Brightline Logistics** (no product data, support storm): flagged from February. The rules waited until May.
- **Why precision looks low:** few accounts in any 15-call list actually churn; even perfect foresight only reaches 16%.

---

## 5. The five requirements

| Requirement | How it's met |
|---|---|
| Messy synthetic data | Generator with a hidden churn process and deliberate mess (section 3.1) |
| A fitted model, not just an LLM | Logistic regression on 29,000+ labelled snapshots. Scores and ranks are identical with the LLM switched off |
| Runs without a human | Scheduled `run` command: ingest → score → diff → brief, logged per step |
| Multi-tenant | `tenant_id` on every row + Postgres row-level security. Tested |
| Traceable | Every score stores its run, model version and feature values. Any signal opens the exact source rows it came from |

---

## 6. Trust, safety and observability

- **No automatic sending:** drafts wait for a CSM, and every approval is logged.
- **Grounded writing:** every talking point cites a fact, and numbers must match the facts. The email can't mention risk or scores.
- **Explainable:** the "Why" bars are the model's own per-account contributions. Signals with implausible directions were removed during training.
- **Point-in-time correct:** late-arriving data is never used by earlier runs.
- **API audit:** every API call records endpoint, status, latency, caller and redacted headers. Swagger docs at `/api/docs`.

---

## 7. What we need from the business

- **Churn definition:** non-renewal only, or downgrades too?
- **Real outcomes** to use as labels, including the two lost renewals.
- **Source of truth** for ARR and renewal date when CRM and billing disagree.
- **CSM weekly capacity**, and playbooks per risk type.
- **Outreach rules:** consent, approved templates, who can send.
- **Trade-off:** is a missed churn worse than a wasted call? This sets the thresholds.

---

## 8. Not in v1, and what's next

- **Out of scope:** real connectors (the synthetic feeds use the same interface), automatic sending, SSO, per-company models, expansion scoring.
- **Next:** AWS deployment with a scheduled run, monitoring and alerts, faster feature computation at 10× scale, and regenerating briefs only when facts change.
