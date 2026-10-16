"""Cross-source identity resolution.

The CRM account (Salesforce Id) is the canonical account: it is the book of
business CSMs are measured on. Every other system's entity is mapped onto it
with an explicit, logged decision:

  explicit_id   the external record carries the CRM id (Stripe metadata,
                product v2 events)                                 conf 1.00
  contact_email exact match on a CRM contact's email               conf 0.98
  domain        corporate email / org domain == account domain    conf 0.95
  domain_stem   workspace slug == account domain stem              conf 0.90
  name_fuzzy    normalised name similarity >= 92                   conf = score
  unresolved    nothing safe; kept in the review queue with the best
                candidate as evidence (never silently guessed)

Free-mail domains (gmail.com etc.) are never used for domain matching.
"""
from __future__ import annotations

import json
import re

from rapidfuzz import fuzz, process

from .normalize import FREE_MAIL, email_domain, registrable

LEGAL = re.compile(r"\b(inc|llc|ltd|group|co|corp|corporation|billing)\b\.?", re.I)
FUZZY_ACCEPT = 92
FUZZY_REVIEW = 80


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = LEGAL.sub(" ", s.lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def rebuild_accounts(conn) -> int:
    """Latest known CRM version of every account -> accounts table."""
    conn.execute("DELETE FROM accounts")
    cur = conn.execute("""
        INSERT INTO accounts (tenant_id, account_id, name, domain, owner_id, segment, industry, arr, seats,
                              contract_start, renewal_date, status)
        SELECT DISTINCT ON (account_id) tenant_id, account_id, name, domain, owner_id, segment, industry,
               arr, seats, contract_start, renewal_date, COALESCE(status, 'active')
        FROM crm_account_versions
        ORDER BY account_id, modified_at DESC""")
    return cur.rowcount


class Resolver:
    def __init__(self, conn):
        accts = conn.execute("SELECT account_id, name, domain FROM accounts").fetchall()
        self.ids = {a["account_id"] for a in accts}
        by_domain: dict[str, set] = {}
        for a in accts:
            if a["domain"]:
                by_domain.setdefault(registrable(a["domain"]), set()).add(a["account_id"])
        self.by_domain = by_domain
        self.by_stem = {}
        for d, ids in by_domain.items():
            self.by_stem.setdefault(d.split(".")[0], set()).update(ids)
        self.names = {a["account_id"]: norm_name(a["name"]) for a in accts}
        self.name_choices = list(self.names.items())
        contacts = conn.execute("""
            SELECT DISTINCT ON (contact_id) contact_id, account_id, email FROM crm_contacts
            ORDER BY contact_id, modified_at DESC""").fetchall()
        self.by_email = {c["email"]: c["account_id"] for c in contacts if c["email"]}

    def _domain(self, dom: str | None, method="domain", conf=0.95):
        dom = registrable(dom)
        if not dom or dom in FREE_MAIL:
            return None
        ids = self.by_domain.get(dom)
        if ids and len(ids) == 1:
            return next(iter(ids)), method, conf, {"domain": dom}
        if ids:
            return None, "unresolved", 0.0, {"reason": "ambiguous_domain", "domain": dom, "candidates": sorted(ids)}
        return None

    def _name(self, name: str | None):
        n = norm_name(name)
        if not n:
            return None
        best = process.extractOne(n, dict(self.name_choices), scorer=fuzz.token_sort_ratio)
        if not best:
            return None
        _, score, acct = best
        if score >= FUZZY_ACCEPT:
            return acct, "name_fuzzy", round(score / 100, 3), {"name": name, "matched": self.names[acct], "score": score}
        if score >= FUZZY_REVIEW:
            return None, "unresolved", 0.0, {"reason": "weak_name_match", "name": name,
                                             "candidate": acct, "score": score}
        return None

    def resolve(self, *, hint=None, email=None, domains=(), stem=None, name=None):
        if hint and hint in self.ids:
            return hint, "explicit_id", 1.0, {"hint": hint}
        if email and email in self.by_email:
            return self.by_email[email], "contact_email", 0.98, {"email": email}
        candidates = []
        for d in ([email_domain(email)] if email else []) + list(domains):
            r = self._domain(d)
            if r and r[0]:
                return r
            if r:
                candidates.append(r)
        if stem:
            ids = self.by_stem.get(stem.lower())
            if ids and len(ids) == 1:
                return next(iter(ids)), "domain_stem", 0.9, {"stem": stem}
        r = self._name(name)
        if r and r[0]:
            return r
        if r:
            candidates.append(r)
        if candidates:
            return candidates[0]
        return None, "unresolved", 0.0, {"reason": "no_match", "name": name, "email": email,
                                         "hint": hint if hint else None}


def resolve_identities(conn) -> dict:
    rebuild_accounts(conn)
    R = Resolver(conn)
    decisions = []

    for c in conn.execute("SELECT customer_id, name, email, account_hint FROM stripe_customers"):
        decisions.append(("stripe_customer", c["customer_id"],
                          R.resolve(hint=c["account_hint"], email=c["email"], name=c["name"])))
    for o in conn.execute("SELECT org_id, name, domains FROM zendesk_orgs"):
        decisions.append(("zendesk_org", o["org_id"], R.resolve(domains=o["domains"], name=o["name"])))
    for w in conn.execute("SELECT workspace_id, workspace_name, account_hint FROM product_workspaces"):
        nm = w["workspace_name"]
        stem = nm if nm and re.fullmatch(r"[a-z0-9-]+", nm) else None
        decisions.append(("product_workspace", w["workspace_id"],
                          R.resolve(hint=w["account_hint"], stem=stem, name=None if stem else nm)))
    # every customer-side email address seen (emails + ticket requesters)
    addrs = conn.execute("""
        SELECT counterpart AS e FROM emails WHERE counterpart IS NOT NULL
        UNION SELECT requester_email FROM tickets WHERE requester_email IS NOT NULL""").fetchall()
    for a in addrs:
        decisions.append(("email_address", a["e"], R.resolve(email=a["e"])))

    rows = [dict(tenant_id=None, source=s, external_id=x, account_id=acct, method=m, confidence=conf,
                 evidence=json.dumps(ev)) for s, x, (acct, m, conf, ev) in decisions]
    tenant = conn.execute("SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    for r in rows:
        r["tenant_id"] = tenant
    from .loader import upsert
    changed = upsert(conn, "identity_links", rows, """
        ON CONFLICT (tenant_id, source, external_id) DO UPDATE SET
          account_id = EXCLUDED.account_id, method = EXCLUDED.method, confidence = EXCLUDED.confidence,
          evidence = EXCLUDED.evidence, decided_at = now()
        WHERE identity_links.account_id IS DISTINCT FROM EXCLUDED.account_id
           OR identity_links.method IS DISTINCT FROM EXCLUDED.method""")
    summary = conn.execute("""
        SELECT source, method, count(*) AS n FROM identity_links GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    return {"decisions": len(rows), "changed": changed,
            "by_method": [dict(r) for r in summary]}
