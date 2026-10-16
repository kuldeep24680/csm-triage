"""Operational evaluation: does the weekly call list catch churn early enough?

AUC says little about the decision a CSM actually makes. Here we replay the
test period week by week: build every CSM's call list (top-capacity by
priority) exactly as the agent would, then check it against what happened.

Same policy (sqrt(ARR) x renewal urgency), different risk estimates:
  model          calibrated churn probability
  rules_ryg      a typical hand-built red/yellow/green points score
  no_risk_signal ARR and renewal date only, roughly what CSMs do today
  oracle         perfect foresight; the best any list of this size could do
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..db import system_tx
from ..features.build import accounts_as_of, coverage, snapshot
from .policy import priority

HORIZON_DAYS = 90
EARLY_WINDOW = (90, 14)   # a churner counts as "caught" if listed 90..14 days before renewal


def _rules(X: pd.DataFrame) -> np.ndarray:
    from .train import rules_baseline
    return rules_baseline(X)


def operational_eval(frames: dict, model, test_start: date, as_of: date) -> dict:
    with system_tx() as conn:
        caps = {r["tenant_id"]: r["csm_weekly_capacity"] for r in
                conn.execute("SELECT tenant_id, csm_weekly_capacity FROM tenants")}
    first = test_start - timedelta(days=EARLY_WINDOW[0])
    first -= timedelta(days=first.weekday())
    dates = []
    d = first
    while d <= as_of - timedelta(days=EARLY_WINDOW[1]):
        dates.append(d)
        d += timedelta(days=7)

    strategies = ("model", "rules_ryg", "no_risk_signal", "oracle")
    listed = {s: [] for s in strategies}        # (tenant, account, date)
    precision_rows = {s: [] for s in strategies}
    lost_all = []
    for t, fr in frames.items():
        o = fr.opps.sort_values("modified_at").groupby("opp_id").tail(1)
        lost = o[(o["stage"] == "Closed Lost")][["account_id", "close_date"]]
        arr_now = fr.acct_versions.sort_values("modified_at").groupby("account_id")["arr"].last()
        for a, r in zip(lost["account_id"], lost["close_date"]):
            if test_start <= r <= as_of:
                lost_all.append((t, a, r, float(arr_now.get(a, 0) or 0)))
        lost_by_acct = lost.groupby("account_id")["close_date"].apply(list).to_dict()
        for d in dates:
            acc = accounts_as_of(fr, d)
            acc = acc[acc["active"]]
            if acc.empty:
                continue
            X = snapshot(fr, d, acc)
            p = model.predict(X, t)
            rules = _rules(X)
            insuff = np.array([coverage(X.loc[i])["insufficient"] for i in X.index])
            base = pd.DataFrame({"owner": acc["owner_id"], "arr": acc["arr"].astype(float),
                                 "dtr": acc["days_to_renewal"], "p": p, "rules": rules,
                                 "insuff": insuff}, index=X.index)
            for s in strategies:
                if s == "oracle":   # perfect foresight: the ceiling for this capacity
                    risk = pd.Series([1.0 if any(d < r <= d + timedelta(days=HORIZON_DAYS)
                                                 for r in lost_by_acct.get(a, [])) else 0.01
                                      for a in base.index], index=base.index)
                else:
                    risk = base["p"] if s == "model" else (base["rules"] + 0.02 if s == "rules_ryg" else 1.0)
                pr = [priority(float(rk), a, dd) for rk, a, dd in
                      zip(np.broadcast_to(risk, len(base)), base["arr"], base["dtr"])]
                b = base.assign(prio=pr)
                if s == "model":     # same rule the agent applies (policy.assign_bands)
                    b = b[~b["insuff"]]
                elif s == "rules_ryg":  # only accounts with at least one red flag
                    b = b[b["rules"] > 0]
                top = b.sort_values("prio", ascending=False).groupby("owner").head(caps.get(t, 15))
                for acct in top.index:
                    listed[s].append((t, acct, d))
                hits = [any(d < r <= d + timedelta(days=HORIZON_DAYS) for r in lost_by_acct.get(a, []))
                        for a in top.index]
                if d >= test_start and d <= as_of - timedelta(days=HORIZON_DAYS):
                    precision_rows[s].append((sum(hits), len(hits), base["owner"].nunique()))

    lost_df = pd.DataFrame(lost_all, columns=["tenant", "account", "renewal", "arr"])
    out = {"weeks_replayed": len(dates), "lost_renewals_in_test": int(len(lost_df)),
           "lost_arr_in_test": round(float(lost_df["arr"].sum()), 2), "strategies": {}}
    for s in strategies:
        L = pd.DataFrame(listed[s], columns=["tenant", "account", "date"])
        caught, caught_arr, leads = 0, 0.0, []
        for row in lost_df.itertuples():
            m = L[(L["tenant"] == row.tenant) & (L["account"] == row.account)]
            win = m[(m["date"] >= row.renewal - timedelta(days=EARLY_WINDOW[0])) &
                    (m["date"] <= row.renewal - timedelta(days=EARLY_WINDOW[1]))]
            if len(win):
                caught += 1
                caught_arr += row.arr
                leads.append((row.renewal - win["date"].min()).days)
        h = sum(x for x, _, _ in precision_rows[s])
        n = sum(y for _, y, _ in precision_rows[s])
        books = sum(z for _, _, z in precision_rows[s])
        out["strategies"][s] = {
            "precision_at_capacity": round(h / n, 4) if n else None,
            "avg_calls_per_csm_week": round(n / books, 1) if books else None,
            "churners_caught_early": round(caught / len(lost_df), 4) if len(lost_df) else None,
            "churned_arr_caught_early": round(caught_arr / lost_df["arr"].sum(), 4) if len(lost_df) else None,
            "median_lead_days": float(np.median(leads)) if leads else None,
        }
    # the two renewals from the brief
    planted = {}
    for name in ("Brightline Logistics", "Kestrel Health"):
        for t, fr in frames.items():
            hit = fr.acct_versions[fr.acct_versions["name"] == name]
            if hit.empty:
                continue
            acct = hit["account_id"].iloc[0]
            row = {}
            for s in strategies:
                L = [d for (tt, a, d) in listed[s] if tt == t and a == acct]
                row[s] = {"first_listed": min(L).isoformat() if L else None, "weeks_listed": len(L)}
            planted[name] = row
    out["planted_lost_renewals"] = planted
    return out
