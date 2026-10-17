# Triage: user flows and validation checklist

What a CSM and a CS manager do in Triage, what they should see at each step, and how to check it works. Setup is in [SETUP.md](SETUP.md).

The simulated "today" is **16 Oct 2026**. Agent runs exist for the 8 Fridays from 28 Aug to 16 Oct, so "what changed since last week" has history.

---

## Who uses it

| Person | Goal | Sees |
|---|---|---|
| **CSM** (~120 accounts) | Decide who to call this week, prepare, send outreach | Only their own book |
| **CS manager** | Check each CSM's list is right and acted on | Every CSM in their company (tenant) |
| **Nobody else** | Triage never emails customers | – |

---

## Flow 1: Monday morning, the CSM's call list

1. **Sign in** with a CSM login (see SETUP.md, step 7).
2. Land on **"Your calls this week"**.
3. Read the summary strip at the top:
   - accounts to call, to watch, and "can't see"
   - accounts in the book
   - expected ARR at risk
4. Work down the **Call this week** list (at most 15 accounts, the CSM's weekly capacity). Each row shows:
   - **rank**: order of priority (risk × revenue × renewal timing)
   - **name, segment, ARR, days to renewal**: shows "(CRM date out of date)" when the CRM renewal date had already passed
   - **risk %**: chance of not renewing, plus an 8-week trend line
   - **up to 3 reasons**: actionable signals only, e.g. "days since the CRM champion last emailed us: 276 days"
   - **"Since last week: …"**: the most important change, e.g. "risk up from 12% to 25%"
   - **"Low risk, but a large renewal is close…"**: why a low-% account is still on the list
   - **draft status**: Draft to review, Email approved, or Draft rejected
5. Below the call list are two more sections:
   - **Can't see: renewing within 120 days**: no product usage and no contact in 90 days. Shown as "?" rather than a score, because no data is not good news.
   - **Watch**: elevated risk that didn't make this week's list.

**Check:**
- [ ] The call list has at most 15 rows, ranked 1, 2, 3, …
- [ ] Every call row has reasons, or the "can't see" explanation.
- [ ] At least some rows show "Since last week".
- [ ] A CSM sees no other CSM's accounts and has no CSM picker.

---

## Flow 2: Preparing for one call (account page)

Click any row. Work top to bottom.

1. **Header**:
   - risk % and band ("Call this week", "Watch", "Healthy", "Can't see")
   - rank in book
   - renewal date
   - a note when the CRM renewal date was stale and the next contract anniversary was used instead
2. **Why**: bars show which signals pushed risk up (red) or down (green).
   - **Click any signal name** to open the evidence drawer: the raw rows (tickets, emails, usage weeks, billing events, CRM versions) that produced that number, as the run saw them.
   - **Hatched rows** arrived *after* the run and were not used. This shows the score only used data that existed at the time.
3. **What changed since last week**:
   - lists the changes, e.g. new urgent tickets, champion quiet for 30+ days, seats reduced, failed payment, risk up or down
   - shows a larger risk trend line
4. **Prep for the call**:
   - a 2-sentence summary and up to 4 talking points
   - each point has **citation chips** (F1, E2 …). Hover a chip to see the fact behind it.
   - **"Facts the brief was allowed to use"** lists everything the writer could see; nothing else is allowed in.
   - if the LLM wrote something the facts don't support, it is removed, and the page says "N claim(s) were removed".
5. **Draft email**:
   - **To** is the champion contact.
   - The subject and message can be edited.
   - **Approve draft**: the status becomes Approved, and an **"Open in Gmail"** button opens a Gmail draft in a new tab with To, Subject and Body filled in.
   - **Reject draft**: the status becomes Rejected.
   - Triage never sends anything. The page says so.
6. **Right column**:
   - **What we can see**: is there product usage? recent contact? Which Stripe / Zendesk / product records are linked, and how they were matched (and with what confidence)?
   - **Recent activity**: 120-day timeline of tickets, emails, and billing events.
   - **How this score was made**: run ID, model version, contacts (the champion is marked). Open **"All 24 feature values"** and click "rows" next to any feature for its evidence.

**Check:**
- [ ] Clicking a signal opens the drawer with rows (or "No source rows…" when the signal is about absence, e.g. no usage).
- [ ] Every talking point has at least one citation chip, and hovering shows a fact.
- [ ] The draft email never mentions "risk", "churn", "score" or "model".
- [ ] After approving, the draft is read-only, shows "Approved by <you> on <date>", and the list row shows "Email approved".
- [ ] Approving the same draft again is impossible (no buttons; the API returns "draft already approved").

---

## Flow 3: The CS manager

1. Sign in as `manager@northwind.example`.
2. The **Call lists** page opens on **Whole team**: every CSM's call list in one ranked view, with the owner shown on each row.
3. Use the **CSM picker** (top right) to see one CSM's list exactly as they see it.
4. The **Team** page has one row per CSM:
   - accounts, call / watch / can't-see counts
   - expected ARR at risk
   - drafts approved vs waiting
   - click a name to jump to that CSM's list

**Check:**
- [ ] The Team page totals look consistent with the whole-team strip.
- [ ] The picker shows each CSM's own list of at most 15 calls.

---

## Flow 4: Trust and transparency pages (manager or CSM)

| Page | What it answers | What to look for |
|---|---|---|
| **Agent runs** | Did the unattended runs happen and succeed? | 8 runs, all "succeeded"; duration; accounts scored; call / watch / can't-see counts; briefs and who wrote them (`template-v1` or `openai:gpt-4o-mini`); claims removed. **Steps** shows each stage with timings and errors |
| **Model** | Is the model any good, and better than the alternatives? | Replay table: Triage model 75% of churners listed early vs 55% (rules) and 30% (ARR + date). Calibration dots close to the dashed line. "The two renewals we lost" (Northwind only). The weights table |
| **Data quality** | Can we trust the inputs? | Records received per source, duplicates removed, late arrivals, how records were matched to accounts, the "needs a human" queue of unmatched records, rejected lines with reasons, accounts we can't see, stale CRM dates |

---

## Stories planted in the data (Northwind)

Use these to check that the system tells a sensible story.

| Account | What happened (hidden truth) | What Triage should show |
|---|---|---|
| **Orchid Freight** | Champion left in Aug 2026; competitor evaluation in Sep; sparse telemetry | On its CSM's call list; risk rising over 4 weeks (about 10% → 14% → 17% → 25%); renewal on 1 Dec 2026 |
| **Summit Dental Group** | Budget cut (seats reduced) in Sep 2026, then a support issue | On the call list on 16 Oct; seat reduction among the reasons; renewal 20 Nov 2026 |
| **Kestrel Health** | Champion went silent in Apr 2026; churned at its 21 Sep 2026 renewal | No longer in the active book. **Model** page: listed by Triage from 8 Jun; never listed by the rules |
| **Brightline Logistics** | No product telemetry, support storm from Apr 2026; churned 15 Aug 2026 | **Model** page: listed from Feb; rules only from May |

To find Orchid Freight or Summit Dental:
1. Open the **Whole team** list.
2. Use your browser's find (Cmd+F) for the name.
3. If it isn't there, pick the owning CSM in the picker (the owner's name appears on the account page).

---

## Security and isolation checks

- [ ] **CSM boundary.** Signed in as a CSM, paste the URL of an account from another CSM's book (copy it while signed in as the manager). You should get an error ("not in your book").
- [ ] **Tenant boundary.** Signed in as `manager@helios.example`, paste a Northwind account URL. You should get "account not found".
- [ ] **Model page boundary.** The Model page for Helios or Quill shows only that company's metrics, and no "two renewals we lost" section.
- [ ] **Session expiry.** Signing out, or a token older than 12 hours, returns you to the sign-in page.

---

## What Triage deliberately does not do (v1)

- Send emails, or take any action on a customer's behalf
- Let users change scores or rankings manually
- Real connectors to Salesforce, Zendesk, Gmail, product analytics or Stripe (the synthetic landing files use the same interface)
- SSO, or an editable review workflow for unmatched records (the queue is visible only)
