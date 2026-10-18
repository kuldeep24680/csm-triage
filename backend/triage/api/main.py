"""HTTP API for the CSM web app.

Every handler that touches tenant data opens ``tenant_tx(user.tenant_id)``:
Postgres row-level security then guarantees the query cannot see another
tenant's rows, even if the SQL here had a bug.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import settings
from ..db import system_tx, tenant_tx
from ..features.build import FEATURES
from ..observability import setup as setup_telemetry
from .audit import audit_middleware, start_writer, stop_writer
from .auth import User, current_user, login

setup_telemetry("triage-api")
log = logging.getLogger("triage.api")

TAGS = [
    {"name": "auth", "description": "Sign in. Returns a JWT; send it as `Authorization: Bearer <token>`. "
                                     "Use the **Authorize** button above with a token from `POST /api/auth/login`."},
    {"name": "call list", "description": "The weekly ranked call list for a CSM (or the whole team for a manager)."},
    {"name": "accounts", "description": "Account detail, score drivers, brief, draft, timeline and the raw "
                                        "evidence behind every feature value."},
    {"name": "outreach", "description": "Approve or reject drafted customer emails. Nothing is ever sent by Triage."},
    {"name": "agent & model", "description": "Unattended agent runs and the active model card."},
    {"name": "data quality", "description": "Ingestion, de-duplication, identity matching and coverage."},
    {"name": "observability", "description": "Health check and the API request audit log."},
]

app = FastAPI(
    title="Triage API",
    version="1.0",
    description=(
        "Ranks each CSM's accounts by churn risk, revenue and renewal urgency, explains why, and drafts "
        "outreach for human approval.\n\n"
        "**Tenancy:** the tenant comes from your token; every query is filtered by Postgres row-level "
        "security.\n\n**Demo login:** `manager@northwind.example` / `triage-demo`."),
    openapi_tags=TAGS,
    docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json",
)
app.middleware("http")(audit_middleware)
# Added after the audit middleware so it wraps it: the server span exists when we audit,
# and its trace_id is stored with the request.
FastAPIInstrumentor.instrument_app(app, excluded_urls="api/health")


@app.on_event("startup")
def _start_audit_writer():
    start_writer()


@app.on_event("shutdown")
def _stop_audit_writer():
    stop_writer()


# ----------------------------------------------------------------- helpers
def _latest_run(conn, run_id: str | None = None):
    if run_id:
        r = conn.execute("SELECT * FROM agent_runs WHERE run_id=%s", (run_id,)).fetchone()
    else:
        r = conn.execute("""SELECT * FROM agent_runs WHERE status IN ('succeeded','partial')
                            ORDER BY as_of DESC, started_at DESC LIMIT 1""").fetchone()
    if not r:
        raise HTTPException(404, "no completed agent run yet")
    return r


def _run_meta(r) -> dict:
    return {k: r[k] for k in ("run_id", "as_of", "status", "started_at", "finished_at", "model_version", "trigger")}


def _owner_scope(user: User, owner: str | None) -> str | None:
    """CSMs only ever see their own book; managers can pick a CSM or see all."""
    if user.role == "csm":
        return user.crm_user_id
    return None if owner in (None, "", "all") else owner


def _history(conn, account_ids: list[str], weeks: int = 12) -> dict:
    rows = conn.execute("""
        SELECT DISTINCT ON (s.account_id, s.as_of) s.account_id, s.as_of, s.p_churn, s.band
        FROM account_scores s JOIN agent_runs r USING (run_id)
        WHERE s.account_id = ANY(%s) AND r.status IN ('succeeded','partial')
          AND s.as_of >= (SELECT max(as_of) FROM agent_runs) - %s * 7
        ORDER BY s.account_id, s.as_of, r.started_at DESC""", (account_ids, weeks)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["account_id"], []).append({"as_of": r["as_of"], "p": round(r["p_churn"], 4), "band": r["band"]})
    return out


# ------------------------------------------------------------------ public
CONTEXT_FEATURES = {"log_arr", "seg_smb", "seg_ent", "tenure_years", "wau_4w", "usage_weeks_12w", "outbound_30d"}


class LoginIn(BaseModel):
    email: str
    password: str

    model_config = {"json_schema_extra": {"examples": [{"email": "manager@northwind.example",
                                                         "password": "triage-demo"}]}}


@app.post("/api/auth/login", tags=["auth"], summary="Sign in and get a bearer token")
def api_login(body: LoginIn):
    return login(body.email, body.password)


@app.get("/api/health", tags=["observability"], summary="Liveness: database, active model, last run per tenant")
def health():
    with system_tx() as conn:
        conn.execute("SELECT 1")
        model = conn.execute("SELECT model_version, created_at FROM models WHERE is_active").fetchone()
        runs = conn.execute("""SELECT DISTINCT ON (tenant_id) tenant_id, as_of, status, finished_at
                               FROM agent_runs ORDER BY tenant_id, started_at DESC""").fetchall()
        ok = conn.execute("""SELECT tenant_id, max(finished_at) AS last_success,
                                    round(extract(epoch FROM now() - max(finished_at)) / 3600) AS hours_ago
                             FROM agent_runs WHERE status IN ('succeeded','partial') GROUP BY 1""").fetchall()
        stuck = conn.execute("""SELECT count(*) AS n FROM agent_runs WHERE status = 'running'
                                AND heartbeat_at < now() - interval '15 minutes'""").fetchone()["n"]
    return {"ok": True, "model": model, "last_runs": runs, "last_success": ok, "stuck_runs": stuck}


# ---------------------------------------------------------------- the app
@app.get("/api/me", tags=["auth"], summary="Current user, tenant and (for managers) the CSM list")
def me(user: User = Depends(current_user)):
    with tenant_tx(user.tenant_id) as conn:
        t = conn.execute("SELECT tenant_id, name, timezone, csm_weekly_capacity FROM tenants").fetchone()
        csms = conn.execute("SELECT crm_user_id, name, email FROM crm_users ORDER BY name").fetchall() \
            if user.role == "manager" else []
    return {"user": user.__dict__, "tenant": t, "csms": csms}


@app.get("/api/calllist", tags=["call list"], summary="Ranked call list from the latest successful run")
def calllist(owner: str | None = None, run_id: str | None = None, user: User = Depends(current_user)):
    owner = _owner_scope(user, owner)
    with tenant_tx(user.tenant_id) as conn:
        run = _latest_run(conn, run_id)
        rows = conn.execute("""
            SELECT s.account_id, s.owner_id, s.p_churn, s.priority, s.band, s.rank_in_book, s.days_to_renewal,
                   s.arr, s.insufficient_signal, s.drivers, s.changes, a.name, a.segment, a.domain,
                   u.name AS owner_name, d.draft_id, d.status AS draft_status
            FROM account_scores s
            JOIN accounts a ON a.account_id = s.account_id
            LEFT JOIN crm_users u ON u.crm_user_id = s.owner_id
            LEFT JOIN LATERAL (SELECT draft_id, status FROM outreach_drafts d
                               WHERE d.account_id = s.account_id AND d.status <> 'superseded'
                               ORDER BY created_at DESC LIMIT 1) d ON true
            WHERE s.run_id = %s AND (%s::text IS NULL OR s.owner_id = %s)
            ORDER BY s.priority DESC""", (run["run_id"], owner, owner)).fetchall()
        counts: dict = {}
        arr_at_risk = 0.0
        for r in rows:
            counts[r["band"]] = counts.get(r["band"], 0) + 1
            arr_at_risk += float(r["p_churn"]) * float(r["arr"] or 0)
        call = [r for r in rows if r["band"] == "call"]
        watch = [r for r in rows if r["band"] == "watch"][:25]
        unknown = sorted([r for r in rows if r["band"] == "unknown" and (r["days_to_renewal"] or 999) <= 120],
                         key=lambda r: r["days_to_renewal"])
        hist = _history(conn, [r["account_id"] for r in call + watch + unknown])
    with system_tx() as sconn:
        m = sconn.execute("SELECT metrics->>'watch_min_p' AS w FROM models WHERE model_version=%s",
                          (run["model_version"],)).fetchone()
    watch_p = float(m["w"]) if m and m["w"] else None
    for r in call + watch + unknown:
        r["history"] = hist.get(r["account_id"], [])
        # the list shows reasons a CSM can act on; context (size, segment, tenure) stays on the account page
        actionable = [d for d in r["drivers"].get("risk", []) if d["feature"] not in CONTEXT_FEATURES]
        r["drivers"] = {"risk": actionable[:3], "coverage": r["drivers"].get("coverage"),
                        "renewal": r["drivers"].get("renewal")}
        r["low_risk_high_value"] = r["band"] == "call" and watch_p is not None and r["p_churn"] < watch_p
    return {"run": _run_meta(run), "owner": owner, "counts": counts, "accounts_in_book": len(rows),
            "expected_arr_at_risk": round(arr_at_risk, 2), "call": call, "watch": watch, "unknown": unknown}


@app.get("/api/team", tags=["call list"], summary="Per-CSM summary (managers only)")
def team(user: User = Depends(current_user)):
    if user.role != "manager":
        raise HTTPException(403, "managers only")
    with tenant_tx(user.tenant_id) as conn:
        run = _latest_run(conn)
        rows = conn.execute("""
            SELECT s.owner_id, u.name, count(*) AS accounts,
                   count(*) FILTER (WHERE band='call') AS call, count(*) FILTER (WHERE band='watch') AS watch,
                   count(*) FILTER (WHERE band='unknown') AS unknown,
                   round(sum(p_churn * arr)::numeric, 0) AS expected_arr_at_risk,
                   count(d.*) FILTER (WHERE d.status='approved') AS drafts_approved,
                   count(d.*) FILTER (WHERE d.status='pending') AS drafts_pending
            FROM account_scores s LEFT JOIN crm_users u ON u.crm_user_id = s.owner_id
            LEFT JOIN outreach_drafts d ON d.run_id = s.run_id AND d.account_id = s.account_id
            WHERE s.run_id = %s GROUP BY 1, 2 ORDER BY expected_arr_at_risk DESC""", (run["run_id"],)).fetchall()
    return {"run": _run_meta(run), "csms": rows}


@app.get("/api/accounts/{account_id}", tags=["accounts"], summary="Account detail: score, drivers, brief, draft, timeline")
def account(account_id: str, run_id: str | None = None, user: User = Depends(current_user)):
    with tenant_tx(user.tenant_id) as conn:
        a = conn.execute("""SELECT a.*, u.name AS owner_name FROM accounts a
                            LEFT JOIN crm_users u ON u.crm_user_id = a.owner_id WHERE account_id=%s""",
                         (account_id,)).fetchone()
        if not a:
            raise HTTPException(404, "account not found")
        if user.role == "csm" and a["owner_id"] != user.crm_user_id:
            raise HTTPException(403, "not in your book")
        run = _latest_run(conn, run_id)
        score = conn.execute("SELECT * FROM account_scores WHERE run_id=%s AND account_id=%s",
                             (run["run_id"], account_id)).fetchone()
        feats = conn.execute("""SELECT features, feature_version FROM feature_snapshots
                                WHERE account_id=%s AND as_of=%s""", (account_id, run["as_of"])).fetchone()
        brief = conn.execute("""SELECT b.*, r.as_of FROM briefs b JOIN agent_runs r USING (run_id)
                                WHERE b.account_id=%s ORDER BY r.as_of DESC, r.started_at DESC LIMIT 1""",
                             (account_id,)).fetchone()
        draft = conn.execute("""SELECT * FROM outreach_drafts WHERE account_id=%s AND status <> 'superseded'
                                ORDER BY created_at DESC LIMIT 1""", (account_id,)).fetchone()
        contacts = conn.execute("""SELECT DISTINCT ON (contact_id) contact_id, name, email, title, is_champion
                                   FROM crm_contacts WHERE account_id=%s ORDER BY contact_id, modified_at DESC""",
                                (account_id,)).fetchall()
        links = conn.execute("""SELECT source, external_id, method, confidence, evidence, decided_at
                                FROM identity_links WHERE account_id=%s ORDER BY source""", (account_id,)).fetchall()
        hist = _history(conn, [account_id], weeks=52).get(account_id, [])
        timeline = _timeline(conn, account_id, run["as_of"])
    features = None
    if feats:
        features = [{"feature": k, "label": FEATURES.get(k, k), "value": v} for k, v in feats["features"].items()]
    return {"account": a, "run": _run_meta(run), "score": score, "features": features, "history": hist,
            "brief": brief, "draft": draft, "contacts": contacts,
            "links": [l for l in links if l["source"] != "email_address"],
            "email_addresses_linked": sum(1 for l in links if l["source"] == "email_address"),
            "timeline": timeline}


def _timeline(conn, account_id: str, as_of: date, days: int = 120) -> list[dict]:
    since = as_of - timedelta(days=days)
    ev = []
    for r in conn.execute("""
            SELECT t.ticket_id, t.subject, t.priority, t.category, t.created_at, t.solved_at, t.csat
            FROM tickets t
            LEFT JOIN identity_links lo ON lo.source='zendesk_org' AND lo.external_id=t.org_id
            LEFT JOIN identity_links le ON le.source='email_address' AND le.external_id=t.requester_email
            WHERE COALESCE(lo.account_id, le.account_id)=%s AND t.created_at >= %s AND t.created_at < %s""",
                          (account_id, since, as_of)):
        ev.append({"at": r["created_at"], "source": "zendesk", "kind": "ticket",
                   "title": r["subject"], "detail": f"{r['priority']} priority, {r['category'].replace('_', ' ')}"
                   + (", solved" if r["solved_at"] else ", open") + (f", CSAT {r['csat']}" if r["csat"] else ""),
                   "ref": r["ticket_id"]})
    for r in conn.execute("""
            SELECT e.message_id, e.direction, e.counterpart, e.subject, e.snippet, e.sent_at FROM emails e
            JOIN identity_links l ON l.source='email_address' AND l.external_id=e.counterpart
            WHERE l.account_id=%s AND e.sent_at >= %s AND e.sent_at < %s""", (account_id, since, as_of)):
        ev.append({"at": r["sent_at"], "source": "gmail", "kind": f"email_{r['direction']}",
                   "title": r["subject"], "detail": r["snippet"], "who": r["counterpart"], "ref": r["message_id"]})
    for r in conn.execute("""
            SELECT b.event_id, b.event_type, b.occurred_at, b.amount_cents, b.quantity, b.prev_quantity FROM billing_events b
            JOIN identity_links l ON l.source='stripe_customer' AND l.external_id=b.customer_id
            WHERE l.account_id=%s AND b.occurred_at >= %s AND b.occurred_at < %s
              AND b.event_type <> 'invoice.paid'""", (account_id, since, as_of)):
        detail = (f"${(r['amount_cents'] or 0) / 100:,.0f}" if r["amount_cents"] else
                  f"seats {r['prev_quantity']} → {r['quantity']}" if r["prev_quantity"] else "")
        ev.append({"at": r["occurred_at"], "source": "stripe", "kind": r["event_type"], "title": r["event_type"],
                   "detail": detail, "ref": r["event_id"]})
    ev.sort(key=lambda e: e["at"], reverse=True)
    return ev[:80]


FEATURE_SOURCES = {
    "usage": ["usage_weeks_12w", "wau_4w", "seat_util_4w", "wau_trend", "automation_trend", "weeks_since_usage"],
    "tickets": ["tickets_30d", "tickets_90d", "urgent_tickets_90d", "open_tickets", "bad_csat_90d", "export_tickets_90d"],
    "emails": ["inbound_30d", "inbound_trend", "days_since_inbound", "days_since_champion", "negative_emails_90d",
               "outbound_30d"],
    "billing": ["payment_failures_90d", "seat_contraction_180d"],
    "crm": ["log_arr", "tenure_years", "seg_smb", "seg_ent"],
}


@app.get("/api/accounts/{account_id}/evidence", tags=["accounts"], summary="Raw source rows behind one feature value")
def evidence(account_id: str, feature: str, run_id: str | None = None, user: User = Depends(current_user)):
    """The raw rows behind one feature value, as the run saw them.

    ``visible_to_run`` is false for rows we have now but that had not arrived
    when the run executed (late data): proof the score used only what it could see.
    """
    group = next((g for g, fs in FEATURE_SOURCES.items() if feature in fs), None)
    if not group:
        raise HTTPException(400, "unknown feature")
    with tenant_tx(user.tenant_id) as conn:
        a = conn.execute("SELECT owner_id FROM accounts WHERE account_id=%s", (account_id,)).fetchone()
        if not a or (user.role == "csm" and a["owner_id"] != user.crm_user_id):
            raise HTTPException(404, "account not found")
        run = _latest_run(conn, run_id)
        T = run["as_of"]
        if group == "usage":
            rows = conn.execute("""
                SELECT u.workspace_id, u.week_start, u.active_users, u.sessions, u.f_automations, u.schema_version,
                       u.arrived_at, (u.arrived_at <= %s) AS visible_to_run
                FROM usage_weekly u JOIN identity_links l ON l.source='product_workspace' AND l.external_id=u.workspace_id
                WHERE l.account_id=%s AND u.week_start >= %s AND u.week_start + 7 <= %s
                ORDER BY u.week_start DESC""", (T, account_id, T - timedelta(days=91), T)).fetchall()
            window = "last 12 complete weeks"
        elif group == "tickets":
            rows = conn.execute("""
                SELECT t.ticket_id, t.subject, t.priority, t.category, t.created_at, t.solved_at, t.csat,
                       t.created_arrived_at AS arrived_at, (t.created_arrived_at <= %s) AS visible_to_run
                FROM tickets t
                LEFT JOIN identity_links lo ON lo.source='zendesk_org' AND lo.external_id=t.org_id
                LEFT JOIN identity_links le ON le.source='email_address' AND le.external_id=t.requester_email
                WHERE COALESCE(lo.account_id, le.account_id)=%s AND t.created_at >= %s AND t.created_at < %s
                ORDER BY t.created_at DESC""", (T, account_id, T - timedelta(days=90), T)).fetchall()
            window = "tickets created in the 90 days before the run"
        elif group == "emails":
            rows = conn.execute("""
                SELECT e.message_id, e.direction, e.counterpart, e.subject, e.snippet, e.sent_at, e.arrived_at,
                       (e.arrived_at <= %s) AS visible_to_run
                FROM emails e JOIN identity_links l ON l.source='email_address' AND l.external_id=e.counterpart
                WHERE l.account_id=%s AND e.sent_at >= %s AND e.sent_at < %s
                ORDER BY e.sent_at DESC LIMIT 200""", (T, account_id, T - timedelta(days=365), T)).fetchall()
            window = "emails in the 365 days before the run (30/90-day features use the newest part)"
        elif group == "billing":
            rows = conn.execute("""
                SELECT b.event_id, b.event_type, b.occurred_at, b.amount_cents, b.quantity, b.prev_quantity,
                       b.arrived_at, (b.arrived_at <= %s) AS visible_to_run
                FROM billing_events b JOIN identity_links l ON l.source='stripe_customer' AND l.external_id=b.customer_id
                WHERE l.account_id=%s AND b.occurred_at >= %s AND b.occurred_at < %s
                ORDER BY b.occurred_at DESC""", (T, account_id, T - timedelta(days=180), T)).fetchall()
            window = "billing events in the 180 days before the run"
        else:
            rows = conn.execute("""
                SELECT account_id, name, segment, arr, seats, contract_start, renewal_date, status, owner_id,
                       modified_at, arrived_at, (arrived_at <= %s) AS visible_to_run
                FROM crm_account_versions WHERE account_id=%s ORDER BY modified_at DESC""", (T, account_id)).fetchall()
            window = "CRM account versions (latest visible version is used)"
        snap = conn.execute("""SELECT features FROM feature_snapshots WHERE account_id=%s AND as_of=%s""",
                            (account_id, T)).fetchone()
    return {"feature": feature, "label": FEATURES.get(feature), "group": group, "window": window,
            "run": _run_meta(run), "value": (snap or {}).get("features", {}).get(feature), "rows": rows}


class DraftDecision(BaseModel):
    subject: str | None = None
    body: str | None = None
    reason: str | None = None


def _decide(draft_id: str, status: str, body: DraftDecision, user: User):
    with tenant_tx(user.tenant_id) as conn:
        d = conn.execute("""SELECT d.*, a.owner_id FROM outreach_drafts d JOIN accounts a USING (tenant_id, account_id)
                            WHERE draft_id=%s""", (draft_id,)).fetchone()
        if not d:
            raise HTTPException(404, "draft not found")
        if user.role == "csm" and d["owner_id"] != user.crm_user_id:
            raise HTTPException(403, "not your account")
        if d["status"] != "pending":
            raise HTTPException(409, f"draft already {d['status']}")
        conn.execute("""UPDATE outreach_drafts SET status=%s, decided_by=%s, decided_at=now(),
                        final_subject=%s, final_body=%s WHERE draft_id=%s""",
                     (status, user.email, body.subject or d["subject"], body.body or d["body"], draft_id))
        edited = bool((body.subject and body.subject != d["subject"]) or (body.body and body.body != d["body"]))
        conn.execute("""INSERT INTO audit_log (tenant_id, actor, action, entity, detail)
                        VALUES (%s, %s, %s, %s, %s)""",
                     (user.tenant_id, user.email, f"draft.{status}", f"outreach_drafts:{draft_id}",
                      json.dumps({"account_id": d["account_id"], "edited": edited, "reason": body.reason})))
        return conn.execute("SELECT * FROM outreach_drafts WHERE draft_id=%s", (draft_id,)).fetchone()


@app.post("/api/drafts/{draft_id}/approve", tags=["outreach"], summary="Approve a pending draft (optionally with edits)")
def approve(draft_id: uuid.UUID, body: DraftDecision, user: User = Depends(current_user)):
    """Approval marks the draft ready for the CSM to send from their own mailbox.
    v1 deliberately has no send path: a human always presses send."""
    return _decide(str(draft_id), "approved", body, user)


@app.post("/api/drafts/{draft_id}/reject", tags=["outreach"], summary="Reject a pending draft")
def reject(draft_id: uuid.UUID, body: DraftDecision, user: User = Depends(current_user)):
    return _decide(str(draft_id), "rejected", body, user)


@app.get("/api/runs", tags=["agent & model"], summary="Last 50 agent runs with steps, timings and stats")
def runs(user: User = Depends(current_user)):
    with tenant_tx(user.tenant_id) as conn:
        return conn.execute("""SELECT run_id, as_of, trigger, status, started_at, finished_at, model_version,
                                      steps, stats, error, heartbeat_at, attempts, last_resumed_at,
                                      (status = 'running' AND heartbeat_at < now() - interval '15 minutes') AS stale
                               FROM agent_runs ORDER BY started_at DESC LIMIT 50""").fetchall()


def _require_manager(user: User):
    if user.role != "manager":
        raise HTTPException(403, "only CS managers can start or resume agent runs")


def _background(target, *args):
    """Single-server setup: run the job in a background thread of the API process.
    The per-tenant database lock still prevents overlap with the scheduled run."""
    import contextvars
    import threading
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(target, *args), daemon=True, name="agent-run").start()


class RunRequest(BaseModel):
    as_of: date | None = None


@app.post("/api/runs", tags=["agent & model"], status_code=202,
          summary="Start an agent run now for your tenant (managers only)")
def start_run(body: RunRequest, user: User = Depends(current_user)):
    """Starts a fresh run in the background. Returns 409 if a run for this tenant is already in progress."""
    _require_manager(user)
    from ..agent.runner import run_all
    as_of = body.as_of or (date.today() if settings.use_wall_clock else settings.sim_end_date)
    with tenant_tx(user.tenant_id) as conn:
        busy = conn.execute("""SELECT run_id FROM agent_runs WHERE status='running'
                               AND heartbeat_at > now() - interval '15 minutes' LIMIT 1""").fetchone()
        if busy:
            raise HTTPException(409, f"a run is already in progress ({busy['run_id']})")
        conn.execute("INSERT INTO audit_log (tenant_id, actor, action, detail) VALUES (%s, %s, 'run.start', %s)",
                     (user.tenant_id, user.email, json.dumps({"as_of": str(as_of)})))
    _background(run_all, as_of, user.tenant_id, "manual")
    return {"status": "started", "tenant": user.tenant_id, "as_of": as_of}


@app.post("/api/runs/{run_id}/resume", tags=["agent & model"], status_code=202,
          summary="Resume a failed or abandoned run from its failed step (managers only)")
def resume(run_id: uuid.UUID, user: User = Depends(current_user)):
    """Completed steps (ingest, identity, stored scores) and briefs already written are skipped."""
    _require_manager(user)
    from ..agent.runner import resume_run
    with tenant_tx(user.tenant_id) as conn:
        r = conn.execute("SELECT status FROM agent_runs WHERE run_id=%s", (run_id,)).fetchone()
        if not r:
            raise HTTPException(404, "run not found")
        if r["status"] not in ("failed", "abandoned"):
            raise HTTPException(409, f"only failed or abandoned runs can be resumed (this one is {r['status']})")
        conn.execute("""INSERT INTO audit_log (tenant_id, actor, action, entity) VALUES (%s, %s, 'run.resume', %s)""",
                     (user.tenant_id, user.email, f"agent_runs:{run_id}"))
    _background(resume_run, str(run_id))
    return {"status": "resuming", "run_id": run_id}


@app.get("/api/model", tags=["agent & model"], summary="Active model card and evaluation (this tenant's slice)")
def model_card(user: User = Depends(current_user)):
    with system_tx() as conn:
        m = conn.execute("""SELECT model_version, created_at, algorithm, feature_version, features, train_window, metrics
                            FROM models WHERE is_active""").fetchone()
    if not m:
        raise HTTPException(404, "no active model")
    met = dict(m["metrics"])
    # tenant isolation applies to metadata too: show only this tenant's slice
    met["test_by_tenant"] = {k: v for k, v in met.get("test_by_tenant", {}).items() if k == user.tenant_id}
    met["calibrators"] = {k: v for k, v in met.get("calibrators", {}).items() if k in ("global", user.tenant_id)}
    ops = dict(met.get("operational") or {})
    if user.tenant_id != "northwind":
        ops.pop("planted_lost_renewals", None)
    met["operational"] = ops
    m["metrics"] = met
    m["feature_descriptions"] = FEATURES
    return m


@app.get("/api/data-quality", tags=["data quality"], summary="Ingestion, duplicates, late data, identity matching, coverage")
def data_quality(user: User = Depends(current_user)):
    with tenant_tx(user.tenant_id) as conn:
        landing = conn.execute("""
            SELECT source, count(*) AS files, sum(records) AS records, sum(inserted) AS inserted,
                   sum(duplicates) AS exact_duplicates, sum(rejected) AS rejected, max(arrival_date) AS last_arrival
            FROM landing_files GROUP BY 1 ORDER BY 1""").fetchall()
        normalized = conn.execute("""
            SELECT 'emails' AS source, (SELECT sum(inserted) FROM landing_files WHERE source='gmail') AS raw,
                   (SELECT count(*) FROM emails) AS unique_rows
            UNION ALL SELECT 'usage weeks', (SELECT sum(inserted) FROM landing_files WHERE source='product'),
                             (SELECT count(*) FROM usage_weekly)
            UNION ALL SELECT 'billing events', (SELECT sum(inserted) FROM landing_files WHERE source='stripe'),
                             (SELECT count(*) FROM billing_events)""").fetchall()
        late = conn.execute("""
            SELECT 'product' AS source, count(*) FILTER (WHERE arrived_at > week_start + 15) AS late_rows, count(*) AS rows
              FROM usage_weekly
            UNION ALL SELECT 'zendesk', count(*) FILTER (WHERE created_arrived_at > created_at + interval '3 days'), count(*) FROM tickets
            UNION ALL SELECT 'gmail', count(*) FILTER (WHERE arrived_at > sent_at + interval '3 days'), count(*) FROM emails
            UNION ALL SELECT 'stripe', count(*) FILTER (WHERE arrived_at > occurred_at + interval '3 days'), count(*) FROM billing_events
            """).fetchall()
        schema = conn.execute("SELECT schema_version, count(*) AS rows FROM usage_weekly GROUP BY 1 ORDER BY 1").fetchall()
        identity = conn.execute("""SELECT source, method, count(*) AS n, round(avg(confidence)::numeric, 3) AS avg_conf
                                   FROM identity_links GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
        review = conn.execute("""SELECT source, external_id, evidence, decided_at FROM identity_links
                                 WHERE account_id IS NULL AND source <> 'email_address'
                                 ORDER BY decided_at DESC LIMIT 25""").fetchall()
        rejected = conn.execute("""SELECT source, file_name, line_no, reason, left(payload, 160) AS payload
                                   FROM rejected_records ORDER BY at DESC LIMIT 15""").fetchall()
        stale = conn.execute("""SELECT count(*) AS n FROM account_scores
                                WHERE run_id = (SELECT run_id FROM agent_runs WHERE status IN ('succeeded','partial')
                                                ORDER BY as_of DESC, started_at DESC LIMIT 1)
                                  AND drivers->'renewal'->>'source' = 'inferred_stale_crm'""").fetchone()
        blind = conn.execute("""SELECT count(*) FILTER (WHERE NOT (drivers->'coverage'->>'has_usage')::boolean) AS no_usage,
                                       count(*) FILTER (WHERE insufficient_signal) AS insufficient, count(*) AS total
                                FROM account_scores
                                WHERE run_id = (SELECT run_id FROM agent_runs WHERE status IN ('succeeded','partial')
                                                ORDER BY as_of DESC, started_at DESC LIMIT 1)""").fetchone()
    return {"landing": landing, "semantic_dedup": normalized, "late_arrivals": late, "product_schema_versions": schema,
            "identity": identity, "review_queue": review, "rejected_samples": rejected,
            "stale_crm_renewal_dates": stale["n"], "coverage": blind}


@app.get("/api/audit/requests", tags=["observability"],
         summary="API request audit log for this tenant (managers only)")
def audit_requests(status: int | None = None, route: str | None = None, errors_only: bool = False,
                   user_name: str | None = None, trace_id: str | None = None,
                   limit: int = 100, user: User = Depends(current_user)):
    """Most recent requests first: endpoint, route template, status, latency, caller (user_name,
    email, role), trace_id and (redacted) headers. Filters: exact `status`, `route` substring,
    `errors_only` (>= 400), `user_name` (name or email, substring) and exact `trace_id`.

    Typical use: a user reports a problem -> filter by `user_name` -> copy the `trace_id`
    -> open that trace in your tracing backend / grep the logs for it."""
    if user.role != "manager":
        raise HTTPException(403, "managers only")
    with tenant_tx(user.tenant_id) as conn:
        return conn.execute("""
            SELECT request_id, trace_id, at, method, path, route, query, status, latency_ms, response_bytes,
                   user_name, user_email, role, client_ip, user_agent, request_headers, response_headers, error
            FROM api_requests
            WHERE (%(s)s::int IS NULL OR status = %(s)s)
              AND (%(r)s::text IS NULL OR route ILIKE '%%' || %(r)s || '%%')
              AND (NOT %(e)s OR status >= 400)
              AND (%(u)s::text IS NULL OR user_name ILIKE '%%' || %(u)s || '%%' OR user_email ILIKE '%%' || %(u)s || '%%')
              AND (%(t)s::text IS NULL OR trace_id = %(t)s)
            ORDER BY at DESC LIMIT %(l)s""",
                            {"s": status, "r": route, "e": errors_only, "u": user_name, "t": trace_id,
                             "l": min(max(limit, 1), 1000)}).fetchall()


@app.get("/api/audit/summary", tags=["observability"],
         summary="Per-endpoint traffic, error rate and latency percentiles (managers only)")
def audit_summary(hours: int = 24, user_name: str | None = None, user: User = Depends(current_user)):
    """Grouped by route template over the last `hours` (optionally for one `user_name`):
    request count, 4xx/5xx counts, and p50 / p95 / max latency in milliseconds."""
    if user.role != "manager":
        raise HTTPException(403, "managers only")
    with tenant_tx(user.tenant_id) as conn:
        return conn.execute("""
            SELECT method, COALESCE(route, path) AS route, count(*) AS requests,
                   count(*) FILTER (WHERE status BETWEEN 400 AND 499) AS client_errors,
                   count(*) FILTER (WHERE status >= 500) AS server_errors,
                   round(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::numeric, 1) AS p50_ms,
                   round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::numeric, 1) AS p95_ms,
                   round(max(latency_ms)::numeric, 1) AS max_ms
            FROM api_requests WHERE at > now() - make_interval(hours => %s)
              AND (%s::text IS NULL OR user_name ILIKE '%%' || %s || '%%' OR user_email ILIKE '%%' || %s || '%%')
            GROUP BY 1, 2 ORDER BY requests DESC""",
                            (min(max(hours, 1), 24 * 30), user_name, user_name, user_name)).fetchall()


# --------------------------------------------------------- SPA static files
_dist = Path(settings.static_dir)
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        f = _dist / path
        return FileResponse(f if path and f.is_file() else _dist / "index.html")
