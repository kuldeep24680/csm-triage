"""Synthetic world generator.

A hidden weekly *health* process drives every observable signal. Renewal
outcomes are sampled from that health plus shocks (champion leaves, product
issues, budget cuts, competitor evaluations) plus noise, so the outcome is
learnable but not trivially so.

Output mimics what five real systems would drop into a landing zone:

    data/landing/<tenant>/<source>/<arrival-date>.jsonl
    data/truth/<tenant>.jsonl        # hidden ground truth: never read by the pipeline

Files are partitioned by *arrival* date, not event date. That is how late
data shows up in reality, and it lets the agent replay history night by night.

Deliberate mess:
  * exact duplicates (webhook retries) and semantic duplicates (same email
    exported from two mailboxes)
  * late and out-of-order arrival, including a two-week telemetry outage that
    is backfilled a month later
  * timezones: RFC 2822 offsets (Gmail), naive local timestamps (product v1),
    UTC (Zendesk), epoch seconds (Stripe), +0000 (Salesforce)
  * schema drift: product v1 -> v2 (Jan 2025), Zendesk numeric -> string
    priority (Jun 2024), Salesforce ACV__c -> ARR__c (Sep 2024)
  * identity mismatch: Stripe/Zendesk names differ from CRM names, missing
    domains, personal billing emails, workspace ids with no CRM link
  * sparse accounts: ~15% have little or no product telemetry
  * stale CRM: renewal dates not rolled forward, champions not marked as gone
  * a small share of malformed lines
"""
from __future__ import annotations

import json
import math
import shutil
import string
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import world as W

UTC = timezone.utc
SOURCES = ("salesforce", "zendesk", "gmail", "product", "stripe")

PRODUCT_V2_FROM = date(2025, 1, 6)
ZENDESK_V2_FROM = date(2024, 6, 3)
SF_ARR_RENAMED = date(2024, 9, 1)
TELEMETRY_OUTAGE = (date(2025, 6, 9), date(2025, 6, 22))   # weeks delayed...
TELEMETRY_BACKFILL = date(2025, 7, 14)                       # ...until here


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # Feb 29
        return d.replace(year=d.year + n, day=28)


# ----------------------------------------------------------------------------
@dataclass
class Contact:
    contact_id: str
    name: str
    email: str
    title: str
    is_champion: bool
    active: bool = True


@dataclass
class Account:
    idx: int
    account_id: str
    name: str
    domain: str
    segment: str
    industry: str
    seats: int
    seat_price: float
    contract_start: date
    owner_id: str
    telemetry: str            # full | sparse | none
    contact_tz: str
    workspace_id: str
    zd_org_id: str
    stripe_customer: str
    stripe_sub: str
    base_h: float
    contacts: list[Contact] = field(default_factory=list)
    script: dict | None = None

    @property
    def arr(self) -> float:
        return round(self.seats * self.seat_price, 2)

    def champion(self) -> Contact | None:
        for c in self.contacts:
            if c.is_champion and c.active:
                return c
        return None


class Emitter:
    """Collects records per (source, arrival date) and applies arrival noise."""

    def __init__(self, rng: np.random.Generator, end: date):
        self.rng = rng
        self.end = end
        self.files: dict[tuple[str, date], list] = defaultdict(list)

    def add(self, source: str, event_day: date, payload: dict, *, lag: int = 0,
            late_p: float = 0.0, late_range=(7, 30), dup_p: float = 0.0, dup_variant=None):
        arrival = event_day + timedelta(days=lag)
        if late_p and self.rng.random() < late_p:
            arrival += timedelta(days=int(self.rng.integers(*late_range)))
        if arrival > self.end:
            return  # has not arrived yet as of "today"
        self.files[(source, arrival)].append(payload)
        if dup_p and self.rng.random() < dup_p:
            dup = dup_variant(payload) if dup_variant else payload
            darr = arrival + timedelta(days=int(self.rng.integers(0, 3)))
            if darr <= self.end:
                self.files[(source, darr)].append(dup)


# ----------------------------------------------------------------------------
class TenantSim:
    def __init__(self, tenant: dict, end: date, years: int, seed: int):
        self.t = tenant
        self.end = end
        self.start = add_years(end, -years)
        self.rng = np.random.default_rng(seed)
        self.em = Emitter(self.rng, end)
        self.tz = ZoneInfo(tenant["timezone"])
        self.truth: list[dict] = []
        self._ticket_seq = 10_000
        self._used_ids: set[str] = set()

    # ------------------------------------------------------------- id helpers
    def sfid(self, prefix: str) -> str:
        alphabet = string.ascii_letters + string.digits
        while True:
            s = prefix + "".join(self.rng.choice(list(alphabet), 15))
            if s not in self._used_ids:
                self._used_ids.add(s)
                return s

    def hexid(self, prefix: str, n: int = 14) -> str:
        return prefix + "".join(self.rng.choice(list("0123456789abcdef"), n))

    def pick(self, seq):
        return seq[int(self.rng.integers(0, len(seq)))]

    # --------------------------------------------------------- name variants
    def name_variant(self, name: str) -> str:
        r = self.rng.random()
        base = name
        for suf in (" Inc.", " LLC", " Ltd", " Group", " Co."):
            if base.endswith(suf):
                base = base[: -len(suf)]
        if r < 0.35:
            return name
        if r < 0.5:
            return base.upper()
        if r < 0.65:
            return base + ", Inc."
        if r < 0.75:
            return base.lower()
        if r < 0.85 and len(base) > 6:  # typo: swap two adjacent letters
            i = int(self.rng.integers(1, len(base) - 2))
            return base[:i] + base[i + 1] + base[i] + base[i + 2:]
        return base + " (Billing)"

    def ts_sf(self, d: date) -> str:
        dt = datetime.combine(d, time(int(self.rng.integers(13, 23)), int(self.rng.integers(0, 60))), UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def rand_dt(self, d: date) -> datetime:
        return datetime.combine(d, time(0), UTC) + timedelta(seconds=int(self.rng.integers(0, 86400)))

    # ------------------------------------------------------------- creation
    def make_csms(self):
        self.csms = []
        names = set()
        while len(self.csms) < self.t["n_csms"]:
            n = f"{self.pick(W.FIRST_NAMES)} {self.pick(W.LAST_NAMES)}"
            if n in names:
                continue
            names.add(n)
            email = n.lower().replace(" ", ".") + f"@{self.t['tenant_id']}.example"
            self.csms.append(dict(Id=self.sfid("005"), Name=n, Email=email))
        for u in self.csms:
            self.em.add("salesforce", self.start, {"sobject": "User", **u,
                                                   "LastModifiedDate": self.ts_sf(self.start)})

    def make_accounts(self):
        n = self.t["n_accounts"]
        combos = [(h, tail) for h in W.NAME_HEADS for tail in W.NAME_TAILS]
        order = self.rng.permutation(len(combos))[:n]
        self.accounts: list[Account] = []
        span = (self.end - self.start).days
        for i, ci in enumerate(order):
            head, (tail, industry) = combos[ci]
            legal = self.pick(W.LEGAL_SUFFIXES)
            name = f"{head} {tail}{legal}"
            slug = (head + tail).lower().replace(" ", "")
            domain = slug + self.pick([".com", ".com", ".com", ".io", ".co"])
            seg_r = self.rng.random()
            if seg_r < 0.6:
                segment, seats = "SMB", int(self.rng.integers(5, 40))
            elif seg_r < 0.9:
                segment, seats = "Mid-Market", int(self.rng.integers(40, 300))
            else:
                segment, seats = "Enterprise", int(self.rng.integers(300, 2000))
            # 45% of the base pre-dates the observed window
            if self.rng.random() < 0.45:
                cs = self.start - timedelta(days=int(self.rng.integers(1, 2 * 365)))
            else:
                cs = self.start + timedelta(days=int(span * self.rng.random() ** 0.8 * 0.97))
            tel_r = self.rng.random()
            telemetry = "full" if tel_r < 0.85 else ("sparse" if tel_r < 0.95 else "none")
            base_h = float(self.rng.normal(0.75, 0.55)) + (0.15 if segment == "Enterprise" else 0.0)
            acct = Account(
                idx=i, account_id=self.sfid("001"), name=name, domain=domain, segment=segment,
                industry=industry, seats=seats,
                seat_price=self.t["seat_price"] * float(self.rng.uniform(0.8, 1.1)),
                contract_start=cs, owner_id=self.pick(self.csms)["Id"], telemetry=telemetry,
                contact_tz=self.pick(W.CONTACT_TZS), workspace_id=self.hexid("ws_", 8),
                zd_org_id=str(int(self.rng.integers(10**8, 10**9))),
                stripe_customer=self.hexid("cus_"), stripe_sub=self.hexid("sub_"), base_h=base_h,
            )
            n_contacts = int(self.rng.integers(2, 6))
            for k in range(n_contacts):
                acct.contacts.append(self.new_contact(acct, champion=(k == 0)))
            self.accounts.append(acct)
        if self.t["tenant_id"] == "northwind":
            self.plant_stories()

    def new_contact(self, acct: Account, champion: bool) -> Contact:
        first, last = self.pick(W.FIRST_NAMES), self.pick(W.LAST_NAMES)
        if self.rng.random() < 0.08:
            email = f"{first.lower()}{last.lower()}{int(self.rng.integers(1, 99))}@gmail.com"
        else:
            email = f"{first.lower()}.{last.lower()}@{acct.domain}"
        title = self.pick(W.CONTACT_TITLES[:4] if champion else W.CONTACT_TITLES)
        return Contact(self.sfid("003"), f"{first} {last}", email, title, champion)

    def plant_stories(self):
        """The two lost renewals from the brief, plus two live at-risk accounts.

        Brightline: enterprise, no telemetry (SSO proxy blocks it), support
        storm from April 2026 -> a usage-only RYG shows nothing (or green).
        Kestrel: champion leaves in April 2026, nobody else engages -> silent churn.
        """
        stories = [
            dict(name="Brightline Logistics", domain="brightlinelogistics.com", segment="Enterprise",
                 industry="Logistics", seats=620, cs=date(2023, 8, 15), telemetry="none",
                 shocks=[(date(2026, 4, 20), "issue", 12, -0.7)], outcomes={date(2026, 8, 15): True}),
            dict(name="Kestrel Health", domain="kestrelhealth.com", segment="Mid-Market",
                 industry="Healthcare", seats=180, cs=date(2022, 9, 21), telemetry="full",
                 shocks=[(date(2026, 4, 27), "champion_leaves", 0, -0.5)], outcomes={date(2026, 9, 21): True},
                 no_replacement=True),
            dict(name="Orchid Freight", domain="orchidfreight.com", segment="Mid-Market",
                 industry="Logistics", seats=140, cs=date(2023, 12, 1), telemetry="sparse",
                 shocks=[(date(2026, 8, 3), "champion_leaves", 0, -0.3), (date(2026, 9, 14), "competitor", 0, -0.8)],
                 outcomes={}, no_replacement=True),
            dict(name="Summit Dental Group", domain="summitdental.com", segment="Enterprise",
                 industry="Healthcare", seats=450, cs=date(2022, 11, 20), telemetry="full",
                 shocks=[(date(2026, 9, 7), "budget_cut", 0, -0.2), (date(2026, 9, 21), "issue", 4, -0.6)], outcomes={}),
        ]
        for i, s in enumerate(stories):
            a = self.accounts[i]
            a.name, a.domain, a.segment, a.industry, a.seats = s["name"], s["domain"], s["segment"], s["industry"], s["seats"]
            a.contract_start, a.telemetry, a.base_h = s["cs"], s["telemetry"], 0.7
            a.contacts = [self.new_contact(a, champion=(k == 0)) for k in range(3)]
            for c in a.contacts:  # planted accounts use corporate emails
                c.email = c.email if c.email.endswith(a.domain) else c.name.lower().replace(" ", ".") + "@" + a.domain
            a.script = s

    # -------------------------------------------------------------- emitters
    def sf_account(self, a: Account, d: date, renewal: date | None, status: str, lag: int = 1):
        rec = {"sobject": "Account", "Id": a.account_id, "Name": a.name,
               "Website": self.pick([f"https://www.{a.domain}", a.domain, f"http://{a.domain}/", None]
                                    if self.rng.random() < 0.08 else [f"https://www.{a.domain}"]),
               "OwnerId": a.owner_id, "Segment__c": a.segment, "Industry": a.industry,
               "Seats__c": a.seats, "Contract_Start__c": a.contract_start.isoformat(),
               "Renewal_Date__c": renewal.isoformat() if renewal else None, "Status__c": status,
               "LastModifiedDate": self.ts_sf(d)}
        if d < SF_ARR_RENAMED:
            rec["ACV__c"] = a.arr
        else:
            rec["ARR__c"] = a.arr
        self.em.add("salesforce", d, rec, lag=lag)

    def sf_contact(self, a: Account, c: Contact, d: date):
        self.em.add("salesforce", d, {"sobject": "Contact", "Id": c.contact_id, "AccountId": a.account_id,
                                      "Name": c.name, "Email": c.email, "Title": c.title,
                                      "Champion__c": c.is_champion and c.active,
                                      "LastModifiedDate": self.ts_sf(d)}, lag=1)

    def sf_opp(self, a: Account, opp_id: str, renewal: date, stage: str, d: date, amount: float):
        self.em.add("salesforce", d, {"sobject": "Opportunity", "Id": opp_id, "AccountId": a.account_id,
                                      "Name": f"{a.name} - Renewal {renewal.year}", "Type": "Renewal",
                                      "StageName": stage, "CloseDate": renewal.isoformat(),
                                      "Amount": amount, "LastModifiedDate": self.ts_sf(d)}, lag=1)

    def stripe(self, d: date, etype: str, obj: dict, prev: dict | None = None):
        evt = {"id": self.hexid("evt_", 20), "type": etype,
               "created": int(self.rand_dt(d).timestamp()), "data": {"object": obj}}
        if prev:
            evt["data"]["previous_attributes"] = prev
        self.em.add("stripe", d, evt, late_p=0.01, late_range=(1, 6), dup_p=0.03)

    def product_week(self, a: Account, ws: date, active: int, sessions: int, feats: dict):
        local_midnight = datetime.combine(ws, time(0), self.tz)
        if ws < PRODUCT_V2_FROM:
            rec = {"workspace": a.workspace_id, "workspace_name": self.name_variant(a.name)
                   if self.rng.random() < 0.5 else a.domain.split(".")[0],
                   "period_start": local_midnight.replace(tzinfo=None).isoformat(),  # naive local
                   "wau": active, "sess": sessions,
                   "events": {"report_view": feats["reports"], "integration_sync": feats["integrations"],
                              "automation_run": feats["automations"]}}
        else:
            rec = {"schema_version": 2, "workspace_id": a.workspace_id,
                   "period_start": local_midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "active_users": active, "sessions": sessions, "features": feats}
            if (a.idx * 7919) % 100 < 75:  # 75% of workspaces carry the CRM id
                rec["sf_account_id"] = a.account_id
        lag = 8
        if TELEMETRY_OUTAGE[0] <= ws <= TELEMETRY_OUTAGE[1]:
            lag = (TELEMETRY_BACKFILL - ws).days
        self.em.add("product", ws, rec, lag=lag, late_p=0.04, late_range=(7, 28), dup_p=0.01)

    def email(self, a: Account, d: date, inbound: bool, contact: Contact, subject: str, snippet: str,
              thread: str | None = None):
        owner = next(u for u in self.csms if u["Id"] == a.owner_id)
        sent = self.rand_dt(d)
        tz = ZoneInfo(a.contact_tz if inbound else self.t["timezone"])
        rec = {"mailbox": owner["Email"], "message_id": f"<{self.hexid('', 24)}@mail.gmail.com>",
               "thread_id": thread or self.hexid("", 16),
               "headers": {"From": contact.email if inbound else owner["Email"],
                           "To": owner["Email"] if inbound else contact.email,
                           "Date": format_datetime(sent.astimezone(tz)), "Subject": subject},
               "snippet": snippet}
        manager_box = f"cs-team@{self.t['tenant_id']}.example"
        self.em.add("gmail", d, rec, lag=int(self.rng.integers(0, 2)), dup_p=0.06,
                    dup_variant=lambda r: {**r, "mailbox": manager_box})

    def ticket(self, a: Account, d: date, category: str, priority: str, h: float, issue: bool):
        self._ticket_seq += 1
        tid = self._ticket_seq
        created = self.rand_dt(d)
        requester = self.pick([c for c in a.contacts if c.active] or a.contacts)
        if d < ZENDESK_V2_FROM:
            pr = {"urgent": 1, "high": 2, "normal": 3, "low": 4}[priority]
        else:
            pr = priority
        base = {"type": "ticket", "id": tid,
                "organization_id": None if self.rng.random() < 0.05 else int(a.zd_org_id),
                "requester": {"email": requester.email},
                "subject": self.pick(W.TICKET_SUBJECTS[category]), "tags": [category], "priority": pr}
        self.em.add("zendesk", d, {**base, "status": "new",
                                   "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "updated_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")},
                    lag=int(self.rng.integers(0, 2)), late_p=0.02, late_range=(10, 40), dup_p=0.02)
        hours = float(self.rng.lognormal(math.log(18 if not issue else 60), 0.8))
        solved = created + timedelta(hours=hours)
        r = self.rng.random()
        p_bad = sigmoid(-1.6 - 1.4 * h + (1.2 if issue else 0.0))
        if r < 0.45:
            csat = "bad" if self.rng.random() < p_bad else "good"
        else:
            csat = "offered" if r < 0.7 else None
        solved_rec = {**base, "status": "solved", "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "updated_at": solved.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "solved_at": solved.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "satisfaction_rating": {"score": csat} if csat else None}
        self.em.add("zendesk", solved.date(), solved_rec, lag=int(self.rng.integers(0, 2)),
                    late_p=0.02, late_range=(10, 40))

    # ------------------------------------------------------------ simulation
    def simulate(self, a: Account):
        rng = self.rng
        start = max(a.contract_start, self.start)
        script = a.script or {}
        scripted_shocks = {monday(d): (kind, dur, b) for d, kind, dur, b in script.get("shocks", [])}

        renewals = []
        k = 1
        while True:
            r = add_years(a.contract_start, k)
            if r > self.end + timedelta(days=400):
                break
            if r > start:
                renewals.append(r)
            k += 1
        next_renewal = renewals[0]

        # ---- initial CRM + Stripe + Zendesk records
        self.sf_account(a, start, next_renewal, "Active")
        for c in a.contacts:
            self.sf_contact(a, c, start)
        email_r = self.rng.random()
        bill_email = (f"billing@{a.domain}" if email_r < 0.85 else
                      (f"finance@{a.domain}" if email_r < 0.9 else f"{a.domain.split('.')[0]}.ap@gmail.com"))
        meta = {"sf_account_id": a.account_id} if (a.idx * 104729) % 100 < 70 else {}
        self.stripe(start, "customer.created", {"id": a.stripe_customer, "object": "customer",
                                                "name": self.name_variant(a.name), "email": bill_email,
                                                "metadata": meta})
        domain_names = [] if self.rng.random() < 0.12 else [self.pick([a.domain, a.domain, f"support.{a.domain}"])]
        self.em.add("zendesk", start, {"type": "organization", "id": int(a.zd_org_id),
                                       "name": self.name_variant(a.name), "domain_names": domain_names,
                                       "created_at": self.rand_dt(start).strftime("%Y-%m-%dT%H:%M:%SZ")}, lag=1)

        h = a.base_h + float(rng.normal(0, 0.3))
        base = a.base_h
        state = dict(champ_left=None, new_champ_at=None, issue_until=None, budget_cut=None,
                     competitor_until=None, competitor_at=None)
        shocks_log = []
        renewal_log = []
        h_hist: dict[date, float] = {}
        opp_ids: dict[date, str] = {}
        churned_on = None
        bill_day = a.contract_start.day if a.contract_start.day <= 28 else 28
        recent_fail_until = None

        ws = monday(start)
        while ws <= self.end:
            we = ws + timedelta(days=6)
            # ---------------- shocks
            sh = scripted_shocks.get(ws)
            base_override = None
            if sh:
                sh, base_override = sh[:2], sh[2]
            if not script:
                u = rng.random()
                if u < 0.003 and a.champion():
                    sh = ("champion_leaves", 0)
                elif u < 0.009 and not state["issue_until"]:
                    sh = ("issue", int(rng.integers(3, 9)))
                elif u < 0.0115 and not state["budget_cut"]:
                    sh = ("budget_cut", 0)
                elif u < 0.0135 and not state["competitor_until"]:
                    sh = ("competitor", 0)
                elif u < 0.0165:
                    sh = ("expansion", 0)
            if sh:
                kind, dur = sh
                shocks_log.append({"week": ws.isoformat(), "kind": kind})
                if kind == "champion_leaves" and a.champion():
                    ch = a.champion()
                    ch.active = False
                    h -= 0.5
                    base -= 0.35
                    state["champ_left"] = ws
                    if not script.get("no_replacement") and rng.random() < 0.55:
                        state["new_champ_at"] = ws + timedelta(weeks=int(rng.integers(6, 17)))
                    if rng.random() < 0.3:  # CRM sometimes catches up, weeks later
                        ch.is_champion = False
                        self.sf_contact(a, ch, ws + timedelta(weeks=int(rng.integers(4, 13))))
                elif kind == "issue":
                    state["issue_until"] = ws + timedelta(weeks=dur)
                elif kind == "budget_cut":
                    state["budget_cut"] = ws
                    h -= 0.35
                    base -= 0.2
                    old = a.seats
                    a.seats = max(3, int(a.seats * rng.uniform(0.6, 0.9)))
                    self.stripe(ws + timedelta(days=2), "customer.subscription.updated",
                                {"id": a.stripe_sub, "customer": a.stripe_customer, "quantity": a.seats,
                                 "status": "active"}, {"quantity": old})
                    self.sf_account(a, ws + timedelta(days=int(rng.integers(3, 20))), next_renewal, "Active")
                    recent_fail_until = ws + timedelta(weeks=10)
                elif kind == "competitor":
                    state["competitor_until"] = ws + timedelta(weeks=10)
                    state["competitor_at"] = ws
                elif kind == "expansion":
                    old = a.seats
                    a.seats = int(a.seats * rng.uniform(1.1, 1.4)) + 1
                    h += 0.25
                    self.stripe(ws + timedelta(days=2), "customer.subscription.updated",
                                {"id": a.stripe_sub, "customer": a.stripe_customer, "quantity": a.seats,
                                 "status": "active"}, {"quantity": old})
                    self.sf_account(a, ws + timedelta(days=int(rng.integers(3, 20))), next_renewal, "Active")

            if base_override is not None:
                base = base_override

            if state["new_champ_at"] and ws >= state["new_champ_at"]:
                nc = self.new_contact(a, champion=True)
                a.contacts.append(nc)
                self.sf_contact(a, nc, ws + timedelta(days=int(rng.integers(0, 30))))
                base += 0.2
                state["new_champ_at"] = None
                state["champ_left"] = None

            issue = bool(state["issue_until"] and ws < state["issue_until"])
            if state["issue_until"] and ws >= state["issue_until"]:
                state["issue_until"] = None
                base += 0.05
            competitor = bool(state["competitor_until"] and ws < state["competitor_until"])
            if state["competitor_until"] and ws >= state["competitor_until"]:
                state["competitor_until"] = None

            # ---------------- latent health
            h += 0.06 * (base - h) + float(rng.normal(0, 0.07))
            if issue:
                h -= 0.06
            if competitor:
                h -= 0.03
            h_hist[ws] = h

            # ---------------- owner reassignment (CRM noise)
            if rng.random() < 0.0004:
                a.owner_id = self.pick(self.csms)["Id"]
                self.sf_account(a, ws + timedelta(days=1), next_renewal, "Active")

            # ---------------- product telemetry
            emit_usage = a.telemetry == "full" or (a.telemetry == "sparse" and rng.random() < 0.15)
            if emit_usage:
                util = min(0.97, max(0.02, sigmoid(0.4 + 1.3 * h) * 0.9 * float(rng.uniform(0.85, 1.15))))
                active = int(rng.binomial(a.seats, util))
                sessions = int(active * rng.gamma(4.0, 1.0 + 0.4 * sigmoid(h)))
                feats = {"reports": int(rng.poisson(active * 0.6 * sigmoid(h + 0.5))),
                         "integrations": int(rng.poisson(1 + 2 * sigmoid(h))),
                         "automations": int(rng.poisson(active * 0.3 * sigmoid(1.5 * h) * (0.3 if competitor else 1.0)))}
                self.product_week(a, ws, active, sessions, feats)

            # ---------------- support tickets
            seg_rate = {"SMB": 0.08, "Mid-Market": 0.2, "Enterprise": 0.4}[a.segment]
            lam = seg_rate * (1 + 1.2 * max(0.0, 0.3 - h)) + (1.4 if issue else 0.0) + (0.15 if competitor else 0.0)
            for _ in range(int(rng.poisson(lam))):
                if issue and rng.random() < 0.6:
                    cat = self.pick(["bug", "bug", "outage"])
                elif competitor and rng.random() < 0.3:
                    cat = "data_export"
                else:
                    cat = self.pick(["bug", "how_to", "how_to", "billing", "feature_request"])
                pr = ("urgent" if cat == "outage" else
                      self.pick(["high", "high", "urgent", "normal"]) if cat == "bug" and issue else
                      self.pick(["normal", "normal", "high", "low"]) if cat == "bug" else
                      self.pick(["low", "normal"]))
                d = ws + timedelta(days=int(rng.integers(0, 7)))
                if d <= self.end:
                    self.ticket(a, d, cat, pr, h, issue)

            # ---------------- email
            def tone():
                if competitor and rng.random() < 0.4:
                    return "competitor"
                if state["budget_cut"] and (ws - state["budget_cut"]).days < 70 and rng.random() < 0.4:
                    return "budget"
                if h > 0.5:
                    return "positive" if rng.random() < 0.6 else "neutral"
                if h < -0.2:
                    return "negative" if rng.random() < 0.6 else "neutral"
                return "neutral"

            champ = a.champion()
            days_to_r = (next_renewal - ws).days
            n_out = int(rng.poisson(0.2 + (0.4 if 0 <= days_to_r <= 120 else 0) + (0.3 if issue else 0)))
            for _ in range(n_out):
                to = champ or self.pick(a.contacts)
                subj, snip = self.pick(W.EMAIL_OUTBOUND)
                d = ws + timedelta(days=int(rng.integers(0, 5)))
                if d <= self.end:
                    self.email(a, d, False, to, subj, snip)
            if champ:
                for _ in range(int(rng.poisson(0.4 * sigmoid(1.2 * h + 0.5)))):
                    subj, snip = self.pick(W.EMAIL_INBOUND[tone()])
                    d = ws + timedelta(days=int(rng.integers(0, 7)))
                    if d <= self.end:
                        self.email(a, d, True, champ, subj, snip)
            others = [c for c in a.contacts if c.active and not c.is_champion]
            if others and not script.get("no_replacement"):
                for _ in range(int(rng.poisson(0.12))):
                    subj, snip = self.pick(W.EMAIL_INBOUND[tone()])
                    d = ws + timedelta(days=int(rng.integers(0, 7)))
                    if d <= self.end:
                        self.email(a, d, True, self.pick(others), subj, snip)

            # ---------------- billing (monthly invoices)
            for dd in range(7):
                d = ws + timedelta(days=dd)
                if d.day == bill_day and start <= d <= self.end:
                    p_fail = 0.01 + (0.3 if recent_fail_until and d < recent_fail_until else 0) + (0.05 if h < -0.5 else 0)
                    inv = self.hexid("in_", 16)
                    amount = int(a.arr / 12 * 100)
                    if rng.random() < p_fail:
                        self.stripe(d, "invoice.payment_failed", {"id": inv, "customer": a.stripe_customer,
                                                                  "amount_due": amount, "status": "open",
                                                                  "attempt_count": 1})
                        if rng.random() < 0.8:
                            self.stripe(d + timedelta(days=int(rng.integers(3, 9))), "invoice.paid",
                                        {"id": inv, "customer": a.stripe_customer, "amount_due": amount,
                                         "status": "paid", "attempt_count": 2})
                    else:
                        self.stripe(d, "invoice.paid", {"id": inv, "customer": a.stripe_customer,
                                                        "amount_due": amount, "status": "paid", "attempt_count": 1})

            # ---------------- renewal pipeline
            for r in renewals:
                if ws <= r - timedelta(days=120) <= we:
                    opp_ids[r] = self.sfid("006")
                    self.sf_opp(a, opp_ids[r], r, "Prospecting", r - timedelta(days=120), a.arr)
                if r in opp_ids and ws <= r - timedelta(days=30) <= we:
                    self.sf_opp(a, opp_ids[r], r, "Negotiation", r - timedelta(days=30), a.arr)

            if ws <= next_renewal <= we and next_renewal <= self.end:
                r = next_renewal
                recent = [v for kk, v in h_hist.items() if (r - kk).days <= 56]
                hbar = float(np.mean(recent))
                champ_gone = bool(state["champ_left"])
                budget = bool(state["budget_cut"] and (r - state["budget_cut"]).days < 280)
                comp = bool(state["competitor_at"] and (r - state["competitor_at"]).days < 140)
                seg_adj = {"SMB": 0.3, "Mid-Market": 0.0, "Enterprise": -0.4}[a.segment]
                logit = (-1.55 - 1.9 * hbar + 0.9 * champ_gone + 0.6 * budget + 0.5 * comp
                         + seg_adj + self.t["churn_bias"])
                p_true = sigmoid(logit)
                churn = rng.random() < sigmoid(logit + float(rng.normal(0, 0.5)))
                if r in script.get("outcomes", {}):
                    churn = script["outcomes"][r]
                elif script:
                    churn = False
                renewal_log.append({"renewal_date": r.isoformat(), "churned": bool(churn),
                                    "p_true": round(p_true, 4), "h_mean_8w": round(hbar, 3),
                                    "champion_gone": champ_gone, "budget_cut": budget, "competitor": comp})
                if r not in opp_ids:  # created when the window began inside the 120d
                    opp_ids[r] = self.sfid("006")
                    self.sf_opp(a, opp_ids[r], r, "Negotiation", max(start, r - timedelta(days=20)), a.arr)
                close_lag = int(rng.integers(0, 15)) + (int(rng.integers(0, 30)) if churn and rng.random() < 0.3 else 0)
                self.sf_opp(a, opp_ids[r], r, "Closed Lost" if churn else "Closed Won",
                            r + timedelta(days=close_lag), a.arr)
                if churn:
                    churned_on = r
                    self.stripe(r, "customer.subscription.deleted",
                                {"id": a.stripe_sub, "customer": a.stripe_customer, "status": "canceled"})
                    self.sf_account(a, r + timedelta(days=int(rng.integers(1, 21))), r, "Churned")
                    break
                renewals = [x for x in renewals if x > r]
                next_renewal = renewals[0]
                # CRM hygiene: usually rolled forward within ~10 days, sometimes 1-2 months late
                upd = r + timedelta(days=int(rng.integers(30, 61)) if rng.random() < 0.15 else int(rng.integers(0, 11)))
                self.sf_account(a, upd, next_renewal, "Active")
                base += 0.1  # a renewal is a reset moment
            ws += timedelta(days=7)

        # post-churn usage tail: a few weeks of dwindling activity
        if churned_on and a.telemetry == "full":
            for j in range(int(rng.integers(0, 4))):
                w = monday(churned_on) + timedelta(weeks=j + 1)
                if w <= self.end:
                    act = max(0, int(a.seats * 0.05 / (j + 1)))
                    self.product_week(a, w, act, act * 2, {"reports": act, "integrations": 0, "automations": 0})

        self.truth.append({"account_id": a.account_id, "name": a.name, "segment": a.segment,
                           "telemetry": a.telemetry, "contract_start": a.contract_start.isoformat(),
                           "observed_from": start.isoformat(), "churned_on": churned_on.isoformat() if churned_on else None,
                           "renewals": renewal_log, "shocks": shocks_log, "planted": bool(script),
                           "ids": {"stripe_customer": a.stripe_customer, "zendesk_org": a.zd_org_id,
                                   "product_workspace": a.workspace_id}})

    # ----------------------------------------------------------------- output
    def run(self):
        self.make_csms()
        self.make_accounts()
        for a in self.accounts:
            self.simulate(a)

    def write(self, out: Path) -> dict:
        tid = self.t["tenant_id"]
        land = out / "landing" / tid
        if land.exists():
            shutil.rmtree(land)
        counts = defaultdict(int)
        malformed = 0
        for (source, arrival), recs in sorted(self.em.files.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            d = land / source
            d.mkdir(parents=True, exist_ok=True)
            # shuffle within a file: arrival order != event order
            idx = self.rng.permutation(len(recs))
            with open(d / f"{arrival.isoformat()}.jsonl", "w") as f:
                for i in idx:
                    line = json.dumps(recs[i], separators=(",", ":"))
                    if self.rng.random() < 0.0008:  # truncated / corrupt line
                        line = line[: max(5, len(line) // 2)]
                        malformed += 1
                    f.write(line + "\n")
            counts[source] += len(recs)
        (out / "truth").mkdir(parents=True, exist_ok=True)
        with open(out / "truth" / f"{tid}.jsonl", "w") as f:
            for row in self.truth:
                f.write(json.dumps(row) + "\n")
        meta = {"tenant_id": tid, "name": self.t["name"], "timezone": self.t["timezone"],
                "capacity": self.t["capacity"], "records": dict(counts), "malformed": malformed,
                "accounts": len(self.accounts)}
        (out / "landing" / tid / "_tenant.json").write_text(json.dumps(meta, indent=2))
        return meta


def generate(out: Path, end: date, years: int, seed: int, tenants: list[str] | None = None) -> list[dict]:
    metas = []
    for i, t in enumerate(W.TENANTS):
        if tenants and t["tenant_id"] not in tenants:
            continue
        sim = TenantSim(t, end, years, seed * 1000 + i)
        sim.run()
        metas.append(sim.write(out))
    return metas
