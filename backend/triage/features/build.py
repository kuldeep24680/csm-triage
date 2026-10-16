"""Point-in-time feature computation.

One function, ``FeatureBuilder.snapshot(as_of)``, is used for training,
backtesting and nightly scoring, so there is no train/serve skew.

Point-in-time rule: a row is visible at ``as_of`` only if
    event_time < as_of        (it happened before the snapshot)  AND
    arrived_at <= as_of       (we had actually received it by then).
The second condition matters: late-arriving data (e.g. the June 2025
telemetry outage backfilled in July) must not leak into a snapshot taken
before it arrived.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

FEATURE_VERSION = "fv1"

# Feature name -> human description (used in drivers, briefs and the UI)
FEATURES: dict[str, str] = {
    "usage_weeks_12w": "weeks with product usage data in the last 12",
    "wau_4w": "weekly active users (4-week average)",
    "seat_util_4w": "share of paid seats active (4-week average)",
    "wau_trend": "change in active users, last 4 weeks vs the 8 before",
    "automation_trend": "change in automation runs, last 4 weeks vs the 8 before",
    "weeks_since_usage": "weeks since any product usage was seen",
    "tickets_30d": "support tickets opened in the last 30 days",
    "tickets_90d": "support tickets opened in the last 90 days",
    "urgent_tickets_90d": "high/urgent tickets in the last 90 days",
    "open_tickets": "tickets currently open",
    "bad_csat_90d": "negative CSAT ratings in the last 90 days",
    "export_tickets_90d": "data-export requests in the last 90 days",
    "inbound_30d": "emails from the customer in the last 30 days",
    "inbound_trend": "change in customer emails, last 30 days vs 90-day average",
    "days_since_inbound": "days since the customer last emailed us",
    "days_since_champion": "days since the CRM champion last emailed us",
    "negative_emails_90d": "customer emails with negative/at-risk language (90 days)",
    "outbound_30d": "emails we sent in the last 30 days",
    "payment_failures_90d": "failed payments in the last 90 days",
    "seat_contraction_180d": "largest seat reduction in the last 180 days (share)",
    "log_arr": "ARR (log)",
    "tenure_years": "years as a customer",
    "seg_smb": "segment: SMB",
    "seg_ent": "segment: Enterprise",
}
FEATURE_COLS = list(FEATURES)

NEGATIVE_LEXICON = re.compile(
    r"reliability|escalat|failed|third time|reviewing all our tools|adoption has been low|not sure the team"
    r"|export|notice period|reduce seats|budgets? (?:were|was) cut|hold on new spend|payment may be delayed",
    re.I)

CAP_DAYS = 365


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d).tz_localize("UTC")


def _log_ratio(recent: pd.Series, prior: pd.Series) -> pd.Series:
    return np.log((recent + 1.0) / (prior + 1.0))


def urgency(days: float | int | None) -> float:
    """How actionable a renewal is right now (see README: renewal proximity)."""
    if days is None or (isinstance(days, float) and np.isnan(days)):
        return 0.3
    if days < 0:
        return 0.8
    if days <= 14:
        return 0.8
    if days <= 120:
        return 1.0
    if days <= 180:
        return 0.6
    if days <= 270:
        return 0.3
    return 0.1


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def next_anniversary(start: date, as_of: date) -> date:
    k = max(1, as_of.year - start.year)
    r = add_years(start, k)
    while r < as_of:
        k += 1
        r = add_years(start, k)
    while k > 1 and add_years(start, k - 1) >= as_of:
        k -= 1
        r = add_years(start, k)
    return r


@dataclass
class TenantFrames:
    tenant_id: str
    acct_versions: pd.DataFrame
    contacts: pd.DataFrame
    opps: pd.DataFrame
    tickets: pd.DataFrame
    emails: pd.DataFrame
    usage: pd.DataFrame
    billing: pd.DataFrame


def _df(conn, sql: str) -> pd.DataFrame:
    cur = conn.execute(sql)
    cols = [c.name for c in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def load_frames(conn, tenant_id: str) -> TenantFrames:
    fr = TenantFrames(
        tenant_id=tenant_id,
        acct_versions=_df(conn, """SELECT account_id, name, owner_id, segment, arr, seats, contract_start,
                                          renewal_date, status, modified_at, arrived_at
                                   FROM crm_account_versions"""),
        contacts=_df(conn, "SELECT contact_id, account_id, email, is_champion, modified_at, arrived_at FROM crm_contacts"),
        opps=_df(conn, """SELECT opp_id, account_id, opp_type, stage, close_date, modified_at, arrived_at
                          FROM crm_opportunities"""),
        tickets=_df(conn, """
            SELECT t.ticket_id, COALESCE(lo.account_id, le.account_id) AS account_id, t.priority, t.category,
                   t.created_at, t.created_arrived_at, t.solved_at, t.solved_arrived_at, t.csat
            FROM tickets t
            LEFT JOIN identity_links lo ON lo.source = 'zendesk_org' AND lo.external_id = t.org_id
            LEFT JOIN identity_links le ON le.source = 'email_address' AND le.external_id = t.requester_email"""),
        emails=_df(conn, """
            SELECT e.message_id, l.account_id, e.direction, e.counterpart, e.sent_at, e.arrived_at, e.snippet
            FROM emails e JOIN identity_links l ON l.source = 'email_address' AND l.external_id = e.counterpart
            WHERE l.account_id IS NOT NULL"""),
        usage=_df(conn, """
            SELECT l.account_id, u.week_start, u.active_users, u.f_automations, u.arrived_at
            FROM usage_weekly u JOIN identity_links l ON l.source = 'product_workspace' AND l.external_id = u.workspace_id
            WHERE l.account_id IS NOT NULL"""),
        billing=_df(conn, """
            SELECT l.account_id, b.event_type, b.occurred_at, b.arrived_at, b.quantity, b.prev_quantity
            FROM billing_events b JOIN identity_links l ON l.source = 'stripe_customer' AND l.external_id = b.customer_id
            WHERE l.account_id IS NOT NULL"""),
    )
    for df, cols in ((fr.acct_versions, ["modified_at", "arrived_at"]), (fr.contacts, ["modified_at", "arrived_at"]),
                     (fr.opps, ["modified_at", "arrived_at"]),
                     (fr.tickets, ["created_at", "created_arrived_at", "solved_at", "solved_arrived_at"]),
                     (fr.emails, ["sent_at", "arrived_at"]), (fr.usage, ["arrived_at"]),
                     (fr.billing, ["occurred_at", "arrived_at"])):
        for c in cols:
            df[c] = pd.to_datetime(df[c], utc=True)
    fr.usage["week_start"] = pd.to_datetime(fr.usage["week_start"]).dt.tz_localize("UTC")
    fr.acct_versions["arr"] = fr.acct_versions["arr"].astype(float)
    fr.emails["negative"] = fr.emails["snippet"].fillna("").str.contains(NEGATIVE_LEXICON)
    return fr


def _visible(df: pd.DataFrame, T: pd.Timestamp, event_col: str, arrived_col: str = "arrived_at") -> pd.DataFrame:
    return df[(df[event_col] < T) & (df[arrived_col] <= T)]


def accounts_as_of(fr: TenantFrames, as_of: date) -> pd.DataFrame:
    """Account attributes as the CRM showed them at as_of, plus derived status."""
    T = _ts(as_of)
    v = _visible(fr.acct_versions, T, "modified_at")
    if v.empty:
        return pd.DataFrame()
    a = v.sort_values("modified_at").groupby("account_id").tail(1).set_index("account_id")
    a = a[pd.to_datetime(a["contract_start"]) <= pd.Timestamp(as_of)]
    # churn known from any source: CRM status, a lost renewal opp, or a Stripe cancellation
    o = _visible(fr.opps, T, "modified_at").sort_values("modified_at").groupby("opp_id").tail(1)
    lost = set(o.loc[o["stage"] == "Closed Lost", "account_id"])
    b = _visible(fr.billing, T, "occurred_at")
    cancelled = set(b.loc[b["event_type"] == "customer.subscription.deleted", "account_id"])
    a["active"] = (a["status"] != "churned") & ~a.index.isin(lost) & ~a.index.isin(cancelled)

    renew, src = [], []
    for cs, rd in zip(a["contract_start"], a["renewal_date"]):
        if rd is not None and not pd.isna(rd) and rd >= as_of:
            renew.append(rd)
            src.append("crm")
        else:
            renew.append(next_anniversary(cs, as_of))
            src.append("inferred_stale_crm" if rd is not None and not pd.isna(rd) else "inferred")
    a["next_renewal"] = renew
    a["renewal_source"] = src
    a["days_to_renewal"] = [(r - as_of).days for r in renew]
    return a


def snapshot(fr: TenantFrames, as_of: date, accounts: pd.DataFrame | None = None) -> pd.DataFrame:
    """Feature matrix (one row per account) as of the start of ``as_of``."""
    T = _ts(as_of)
    acc = accounts if accounts is not None else accounts_as_of(fr, as_of)
    if acc.empty:
        return pd.DataFrame(columns=FEATURE_COLS)
    idx = acc.index
    out = pd.DataFrame(index=idx)

    # ---------------- product usage (complete weeks only)
    u = fr.usage[(fr.usage["week_start"] + pd.Timedelta(days=7) <= T) & (fr.usage["arrived_at"] <= T)]
    age_w = ((T - u["week_start"]).dt.days // 7)
    u = u.assign(age_w=age_w)
    last12 = u[u["age_w"] < 13]
    r4 = u[u["age_w"] < 5]
    p8 = u[(u["age_w"] >= 5) & (u["age_w"] < 13)]
    wk = last12.groupby("account_id")["week_start"].nunique()
    out["usage_weeks_12w"] = wk.reindex(idx).fillna(0)
    wau4 = r4.groupby("account_id")["active_users"].mean()
    wau8 = p8.groupby("account_id")["active_users"].mean()
    au4 = r4.groupby("account_id")["f_automations"].mean()
    au8 = p8.groupby("account_id")["f_automations"].mean()
    out["wau_4w"] = wau4.reindex(idx)
    out["seat_util_4w"] = (out["wau_4w"] / acc["seats"].astype(float).clip(lower=1)).clip(upper=1.5)
    out["wau_trend"] = _log_ratio(wau4.reindex(idx), wau8.reindex(idx))
    out["automation_trend"] = _log_ratio(au4.reindex(idx), au8.reindex(idx))
    last_seen = u.groupby("account_id")["age_w"].min()
    out["weeks_since_usage"] = last_seen.reindex(idx).fillna(52).clip(upper=52)

    # ---------------- support
    t = _visible(fr.tickets, T, "created_at", "created_arrived_at")
    tage = (T - t["created_at"]).dt.days
    solved_known = t["solved_at"].notna() & (t["solved_at"] < T) & (t["solved_arrived_at"] <= T)
    t = t.assign(in30=tage < 30, in90=tage < 90,
                 urg90=(tage < 90) & t["priority"].isin(["urgent", "high"]),
                 open=~solved_known,
                 bad90=solved_known & (t["csat"] == "bad") & ((T - t["solved_at"]).dt.days < 90),
                 exp90=(tage < 90) & (t["category"] == "data_export"))
    g = t.groupby("account_id")[["in30", "in90", "urg90", "open", "bad90", "exp90"]].sum()
    g = g.reindex(idx).fillna(0)
    out["tickets_30d"], out["tickets_90d"], out["urgent_tickets_90d"] = g["in30"], g["in90"], g["urg90"]
    out["open_tickets"], out["bad_csat_90d"], out["export_tickets_90d"] = g["open"], g["bad90"], g["exp90"]

    # ---------------- email
    e = _visible(fr.emails, T, "sent_at")
    eage = (T - e["sent_at"]).dt.days
    e = e.assign(age=eage)
    inbound = e[e["direction"] == "inbound"]
    out["inbound_30d"] = inbound[inbound["age"] < 30].groupby("account_id").size().reindex(idx).fillna(0)
    in90 = inbound[inbound["age"] < 90].groupby("account_id").size().reindex(idx).fillna(0)
    out["inbound_trend"] = _log_ratio(out["inbound_30d"], in90 / 3.0)
    out["days_since_inbound"] = inbound.groupby("account_id")["age"].min().reindex(idx).fillna(CAP_DAYS).clip(upper=CAP_DAYS)
    out["negative_emails_90d"] = inbound[(inbound["age"] < 90) & inbound["negative"]].groupby("account_id").size().reindex(idx).fillna(0)
    outbound = e[e["direction"] == "outbound"]
    out["outbound_30d"] = outbound[outbound["age"] < 30].groupby("account_id").size().reindex(idx).fillna(0)
    c = _visible(fr.contacts, T, "modified_at").sort_values("modified_at").groupby("contact_id").tail(1)
    champs = c.loc[c["is_champion"] & c["email"].notna(), ["account_id", "email"]]
    ci = inbound.merge(champs, left_on=["account_id", "counterpart"], right_on=["account_id", "email"])
    out["days_since_champion"] = ci.groupby("account_id")["age"].min().reindex(idx).fillna(CAP_DAYS).clip(upper=CAP_DAYS)

    # ---------------- billing
    b = _visible(fr.billing, T, "occurred_at")
    bage = (T - b["occurred_at"]).dt.days
    out["payment_failures_90d"] = b[(b["event_type"] == "invoice.payment_failed") & (bage < 90)] \
        .groupby("account_id").size().reindex(idx).fillna(0)
    su = b[(b["event_type"] == "customer.subscription.updated") & (bage < 180) & b["prev_quantity"].notna()]
    contraction = (1 - su["quantity"] / su["prev_quantity"]).clip(lower=0)
    out["seat_contraction_180d"] = contraction.groupby(su["account_id"]).max().reindex(idx).fillna(0)

    # ---------------- account
    out["log_arr"] = np.log1p(acc["arr"].astype(float).fillna(0))
    cs = pd.to_datetime(acc["contract_start"])
    out["tenure_years"] = (pd.Timestamp(as_of) - cs).dt.days / 365.25
    out["seg_smb"] = (acc["segment"] == "SMB").astype(float)
    out["seg_ent"] = (acc["segment"] == "Enterprise").astype(float)
    return out[FEATURE_COLS].astype(float)


def coverage(feats: pd.Series) -> dict:
    """What we can and cannot see for an account. Drives the 'unknown' band."""
    has_usage = feats["usage_weeks_12w"] >= 3
    touch = feats["inbound_30d"] > 0 or feats["days_since_inbound"] < 90 or feats["tickets_90d"] > 0
    return {"has_usage": bool(has_usage), "recent_contact": bool(touch),
            "insufficient": bool(not has_usage and not touch)}


def training_snapshots(fr: TenantFrames, label_cutoff: date, observe_from: date,
                       leads=(28, 56, 84, 112)) -> pd.DataFrame:
    """(account, snapshot date, label) rows from renewal outcomes known by ``label_cutoff``.

    Label = 1 if the renewal opportunity closed lost. Snapshots are taken
    28-112 days before each renewal on the Monday grid; each one only sees
    data that had arrived by then.
    """
    T = _ts(label_cutoff)
    o = fr.opps[(fr.opps["arrived_at"] <= T) & (fr.opps["opp_type"] == "Renewal")]
    o = o.sort_values("modified_at").groupby("opp_id").tail(1)
    o = o[o["stage"].isin(["Closed Won", "Closed Lost"]) & (o["close_date"] <= label_cutoff)]
    rows = []
    min_snap = observe_from + timedelta(days=84)       # need 12 weeks of history
    for acct, r, stage in zip(o["account_id"], o["close_date"], o["stage"]):
        for lead in leads:
            s = r - timedelta(days=lead)
            s = s - timedelta(days=s.weekday())
            if s >= min_snap:
                rows.append((acct, s, r, int(stage == "Closed Lost"), lead))
    return pd.DataFrame(rows, columns=["account_id", "as_of", "renewal_date", "label", "lead_days"])


def build_matrix(fr: TenantFrames, snaps: pd.DataFrame) -> pd.DataFrame:
    """Features for many (account, as_of) pairs, grouped by date for speed."""
    parts = []
    for as_of, grp in snaps.groupby("as_of"):
        acc = accounts_as_of(fr, as_of)
        acc = acc[acc.index.isin(grp["account_id"])]
        if acc.empty:
            continue
        f = snapshot(fr, as_of, acc)
        f = f.join(acc[["owner_id", "arr", "segment", "days_to_renewal", "active"]])
        f["as_of"] = as_of
        f.index.name = "account_id"
        parts.append(grp.merge(f.reset_index(), on=["account_id", "as_of"]))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
