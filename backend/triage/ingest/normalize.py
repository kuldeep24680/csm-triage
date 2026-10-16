"""Source-specific parsing: raw JSON record -> normalised row(s).

Each function returns ``(table, row_dict)`` tuples or raises ``Reject`` with a
reason. Everything here is pure (no DB) so it is unit-testable; schema drift
and timezone rules live in one place per source.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

UTC = timezone.utc
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me"}
ZD_PRIORITY = {1: "urgent", 2: "high", 3: "normal", 4: "low"}


class Reject(Exception):
    pass


def norm_domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    v = v.split("/")[0]
    if v.startswith("www."):
        v = v[4:]
    return v or None


def registrable(domain: str | None) -> str | None:
    """support.acme.com -> acme.com (good enough for .com/.io/.co here)."""
    if not domain:
        return None
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else domain


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].lower().strip(">").strip()


def _req(rec: dict, *keys: str):
    for k in keys:
        if rec.get(k) in (None, ""):
            raise Reject(f"missing {k}")


def _parse_sf_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(UTC)


def _parse_z(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


# ---------------------------------------------------------------- salesforce
def salesforce(rec: dict, ctx: dict):
    so = rec.get("sobject")
    _req(rec, "Id", "LastModifiedDate")
    mod = _parse_sf_ts(rec["LastModifiedDate"])
    if so == "User":
        return [("crm_users", dict(crm_user_id=rec["Id"], name=rec.get("Name") or rec["Id"], email=rec.get("Email")))]
    if so == "Account":
        _req(rec, "Name")
        arr = rec.get("ARR__c", rec.get("ACV__c"))  # field renamed Sep 2024
        return [("crm_account_versions", dict(
            account_id=rec["Id"], name=rec["Name"].strip(), website=rec.get("Website"),
            domain=norm_domain(rec.get("Website")), owner_id=rec.get("OwnerId"),
            segment=rec.get("Segment__c"), industry=rec.get("Industry"),
            arr=float(arr) if arr is not None else None, seats=rec.get("Seats__c"),
            contract_start=_d(rec.get("Contract_Start__c")), renewal_date=_d(rec.get("Renewal_Date__c")),
            status=(rec.get("Status__c") or "Active").lower(), modified_at=mod))]
    if so == "Contact":
        return [("crm_contacts", dict(
            contact_id=rec["Id"], account_id=rec.get("AccountId"), name=rec.get("Name"),
            email=(rec.get("Email") or "").lower() or None, title=rec.get("Title"),
            is_champion=bool(rec.get("Champion__c")), modified_at=mod))]
    if so == "Opportunity":
        _req(rec, "AccountId", "StageName")
        return [("crm_opportunities", dict(
            opp_id=rec["Id"], account_id=rec["AccountId"], opp_type=rec.get("Type"), stage=rec["StageName"],
            close_date=_d(rec.get("CloseDate")), amount=rec.get("Amount"), modified_at=mod))]
    raise Reject(f"unknown sobject {so!r}")


# -------------------------------------------------------------------- zendesk
def zendesk(rec: dict, ctx: dict):
    typ = rec.get("type")
    _req(rec, "id")
    if typ == "organization":
        doms = sorted({registrable(norm_domain(d)) for d in rec.get("domain_names") or [] if d})
        return [("zendesk_orgs", dict(org_id=str(rec["id"]), name=rec.get("name"), domains=doms))]
    if typ == "ticket":
        _req(rec, "created_at")
        pr = rec.get("priority")
        if isinstance(pr, int):  # pre-June-2024 numeric scheme
            pr = ZD_PRIORITY.get(pr)
        elif isinstance(pr, str):
            pr = pr.lower()
        rating = (rec.get("satisfaction_rating") or {}).get("score")
        solved = rec.get("solved_at")
        return [("tickets", dict(
            ticket_id=str(rec["id"]),
            org_id=str(rec["organization_id"]) if rec.get("organization_id") is not None else None,
            requester_email=((rec.get("requester") or {}).get("email") or "").lower() or None,
            subject=rec.get("subject"), category=(rec.get("tags") or [None])[0], priority=pr,
            created_at=_parse_z(rec["created_at"]),
            solved_at=_parse_z(solved) if solved else None,
            csat=rating if rating in ("good", "bad") else None))]
    raise Reject(f"unknown zendesk type {typ!r}")


# ---------------------------------------------------------------------- gmail
def gmail(rec: dict, ctx: dict):
    _req(rec, "message_id", "headers")
    h = rec["headers"]
    if not h.get("Date") or not h.get("From"):
        raise Reject("missing Date/From header")
    sent = parsedate_to_datetime(h["Date"]).astimezone(UTC)   # honours +0530, -0700 ...
    frm = h["From"].lower().strip()
    to = [t.strip().lower() for t in (h.get("To") or "").split(",") if t.strip()]
    ours = ctx["own_domain"]
    outbound = email_domain(frm) == ours
    counterpart = next((t for t in to if email_domain(t) != ours), None) if outbound else frm
    return [("emails", dict(
        message_id=rec["message_id"], thread_id=rec.get("thread_id"), sent_at=sent,
        direction="outbound" if outbound else "inbound", from_email=frm, to_emails=to,
        counterpart=counterpart, subject=h.get("Subject"), snippet=(rec.get("snippet") or "")[:500]))]


# -------------------------------------------------------------------- product
def product(rec: dict, ctx: dict):
    tz: ZoneInfo = ctx["tz"]
    if "workspace" in rec:  # v1: naive timestamp in the tenant's local time
        _req(rec, "workspace", "period_start")
        local = datetime.fromisoformat(rec["period_start"]).replace(tzinfo=tz)
        ev = rec.get("events") or {}
        ws, ver = rec["workspace"], 1
        active, sessions = rec.get("wau"), rec.get("sess")
        feats = (ev.get("report_view"), ev.get("integration_sync"), ev.get("automation_run"))
        name, hint = rec.get("workspace_name"), None
    else:                  # v2: UTC 'Z' timestamp
        _req(rec, "workspace_id", "period_start")
        local = _parse_z(rec["period_start"]).astimezone(tz)
        f = rec.get("features") or {}
        ws, ver = rec["workspace_id"], int(rec.get("schema_version", 2))
        active, sessions = rec.get("active_users"), rec.get("sessions")
        feats = (f.get("reports"), f.get("integrations"), f.get("automations"))
        name, hint = None, rec.get("sf_account_id")
    d = local.date()
    week_start = d - timedelta(days=d.weekday())
    rows = [("usage_weekly", dict(
        workspace_id=ws, week_start=week_start, active_users=active, sessions=sessions,
        f_reports=feats[0], f_integrations=feats[1], f_automations=feats[2], schema_version=ver))]
    rows.append(("product_workspaces", dict(workspace_id=ws, workspace_name=name, account_hint=hint)))
    return rows


# --------------------------------------------------------------------- stripe
def stripe(rec: dict, ctx: dict):
    _req(rec, "id", "type", "created", "data")
    obj = (rec["data"] or {}).get("object") or {}
    prev = (rec["data"] or {}).get("previous_attributes") or {}
    occurred = datetime.fromtimestamp(int(rec["created"]), UTC)
    rows = []
    if rec["type"] == "customer.created":
        rows.append(("stripe_customers", dict(
            customer_id=obj["id"], name=obj.get("name"), email=(obj.get("email") or "").lower() or None,
            account_hint=(obj.get("metadata") or {}).get("sf_account_id"))))
        cust = obj["id"]
    else:
        cust = obj.get("customer")
        if not cust:
            raise Reject("event without customer")
    rows.append(("billing_events", dict(
        event_id=rec["id"], event_type=rec["type"], customer_id=cust, occurred_at=occurred,
        amount_cents=obj.get("amount_due"), quantity=obj.get("quantity"), prev_quantity=prev.get("quantity"))))
    return rows


PARSERS = {"salesforce": salesforce, "zendesk": zendesk, "gmail": gmail, "product": product, "stripe": stripe}


def source_record_id(source: str, rec: dict) -> str | None:
    if source == "gmail":
        return rec.get("message_id")
    if source == "product":
        return f"{rec.get('workspace') or rec.get('workspace_id')}:{rec.get('period_start')}"
    return str(rec.get("Id") or rec.get("id")) if (rec.get("Id") or rec.get("id")) is not None else None


def arrival_ts(d: date) -> datetime:
    return datetime.combine(d, time(0), UTC)
