"""The unattended agent run.

One run = one tenant, one ``as_of`` date. Steps:

  1. ingest       new landing files (idempotent)
  2. identity     re-resolve cross-source identities
  3. score        point-in-time features -> calibrated p(churn) -> drivers
  4. prioritise   p x sqrt(ARR) x renewal urgency; bands per CSM book & capacity
  5. diff         what changed since the previous successful run
  6. brief        grounded LLM brief + customer email draft for the call list
                  (validated; template fallback); drafts wait for CSM approval
  7. record       scores, snapshots, briefs, drafts, run stats

Guarantees:
  * a per-tenant advisory lock stops two runs overlapping;
  * a failure in one tenant never blocks the others;
  * every score row carries run_id + model_version, and the feature snapshot
    it was computed from is stored, so any number in the UI is reproducible;
  * nothing is sent to a customer: drafts are 'pending' until a CSM approves;
  * recovery: a heartbeat after every step/brief batch; a failed run is retried
    automatically once; a failed or abandoned run can be resumed from its first
    unfinished step (completed durable steps and already-written briefs are
    skipped); a run whose process died is marked 'abandoned' by the next run or
    the watchdog, and an alert is sent.
"""
from __future__ import annotations

import contextvars
import io
import json
import logging
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd
from opentelemetry.trace import Status, StatusCode

from .. import alerts
from ..config import settings
from ..db import pool, system_tx, tenant_tx
from ..features.build import FEATURE_VERSION, FEATURES, accounts_as_of, coverage, load_frames, snapshot
from ..ingest.identity import resolve_identities
from ..ingest.loader import ingest_tenant, register_tenants, upsert
from ..observability import record, run_id_var, tenant_var, tracer
from ..model.explain import top_drivers
from ..model.policy import assign_bands, priority
from . import grounding
from .llm import TemplateWriter, get_writer

log = logging.getLogger(__name__)
_model_cache: dict = {}


def load_active_model(version: str | None = None):
    """The active model, or a specific version (a resumed run keeps the model it started with)."""
    with system_tx() as conn:
        if version:
            row = conn.execute("SELECT model_version, artifact FROM models WHERE model_version=%s", (version,)).fetchone()
        else:
            row = conn.execute("SELECT model_version, artifact FROM models WHERE is_active").fetchone()
    if not row:
        raise RuntimeError("no active model: run `python -m triage train` first")
    v = row["model_version"]
    if v not in _model_cache:
        _model_cache[v] = joblib.load(io.BytesIO(row["artifact"]))
    return _model_cache[v]


# ------------------------------------------------------------------- changes
CHANGE_RULES = [
    ("urgent_tickets_90d", lambda a, b: b - a >= 1, lambda a, b: f"{b - a:.0f} new high/urgent support ticket(s)"),
    ("tickets_30d", lambda a, b: b - a >= 2, lambda a, b: f"support tickets in last 30 days rose from {a:.0f} to {b:.0f}"),
    ("payment_failures_90d", lambda a, b: b - a >= 1, lambda a, b: "a new failed payment"),
    ("seat_contraction_180d", lambda a, b: b - a > 0.05, lambda a, b: f"seats reduced by {b * 100:.0f}%"),
    ("days_since_champion", lambda a, b: a < 30 <= b, lambda a, b: f"champion has not emailed in {b:.0f} days"),
    ("negative_emails_90d", lambda a, b: b - a >= 1, lambda a, b: "new customer email with negative or at-risk language"),
    ("export_tickets_90d", lambda a, b: b - a >= 1, lambda a, b: "customer asked how to export their data"),
    ("wau_trend", lambda a, b: a - b >= 0.2, lambda a, b: f"active-user trend fell from {(np.exp(a) - 1) * 100:+.0f}% to {(np.exp(b) - 1) * 100:+.0f}%"),
]


def compute_changes(prev: dict | None, prev_feats: dict | None, cur: dict, feats: dict) -> list[dict]:
    if prev is None:
        return [{"kind": "first_seen", "text": "first week this account was scored"}]
    out = []
    if prev["band"] != cur["band"]:
        out.append({"kind": "band", "text": f"moved from '{prev['band']}' to '{cur['band']}'"})
    dp = cur["p_churn"] - prev["p_churn"]
    if abs(dp) >= 0.05:
        out.append({"kind": "risk", "text": f"risk {'up' if dp > 0 else 'down'} from {prev['p_churn'] * 100:.0f}% "
                                              f"to {cur['p_churn'] * 100:.0f}% since {prev['as_of']}"})
    if prev_feats:
        for f, cond, text in CHANGE_RULES:
            a, b = prev_feats.get(f), feats.get(f)
            if a is None or b is None:
                continue
            if cond(a, b):
                out.append({"kind": f"feature:{f}", "text": text(a, b)})
    return out


# -------------------------------------------------------------------- events
def recent_events(fr, account_id: str, as_of: date, limit: int = 6) -> list[dict]:
    T = pd.Timestamp(as_of).tz_localize("UTC")
    ev = []
    t = fr.tickets[(fr.tickets["account_id"] == account_id) & (fr.tickets["created_at"] < T)
                   & (fr.tickets["created_arrived_at"] <= T) & ((T - fr.tickets["created_at"]).dt.days < 90)]
    for r in t.sort_values("created_at", ascending=False).head(3).itertuples():
        solved = pd.notna(r.solved_at) and r.solved_at < T and r.solved_arrived_at <= T
        csat = ", customer rated it bad" if solved and r.csat == "bad" else ""
        ev.append((r.created_at, {"text": f"Support ticket {r.created_at.date()}: '{_subject(fr, r.ticket_id)}' "
                                          f"({r.priority}, {'solved' if solved else 'open'}{csat}).",
                                  "ref": {"table": "tickets", "id": r.ticket_id, "subject": _subject(fr, r.ticket_id)}}))
    e = fr.emails[(fr.emails["account_id"] == account_id) & (fr.emails["direction"] == "inbound")
                  & (fr.emails["sent_at"] < T) & (fr.emails["arrived_at"] <= T) & ((T - fr.emails["sent_at"]).dt.days < 60)]
    for r in e.sort_values("sent_at", ascending=False).head(3).itertuples():
        ev.append((r.sent_at, {"text": f"Email from {r.counterpart} on {r.sent_at.date()}: \"{r.snippet}\"",
                               "ref": {"table": "emails", "id": r.message_id, "subject": None}}))
    b = fr.billing[(fr.billing["account_id"] == account_id) & (fr.billing["occurred_at"] < T)
                   & (fr.billing["arrived_at"] <= T) & ((T - fr.billing["occurred_at"]).dt.days < 120)]
    b = b.sort_values("occurred_at", ascending=False)
    for r in b[b["event_type"] == "invoice.payment_failed"].head(2).itertuples():
        ev.append((r.occurred_at, {"text": f"A payment failed on {r.occurred_at.date()}.",
                                   "ref": {"table": "billing_events", "type": r.event_type}}))
    for r in b[b["event_type"] == "customer.subscription.updated"].head(2).itertuples():
        if pd.notna(r.prev_quantity) and r.quantity < r.prev_quantity:
            ev.append((r.occurred_at, {"text": f"Seats reduced from {r.prev_quantity:.0f} to {r.quantity:.0f} "
                                               f"on {r.occurred_at.date()}.",
                                       "ref": {"table": "billing_events", "type": r.event_type}}))
    ev.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in ev[:limit]]


def _subject(fr, ticket_id):
    return fr.ticket_subjects.get(ticket_id, "support ticket")


# ---------------------------------------------------------------------- run
def _jsonable(d: dict) -> dict:
    return {k: (None if (isinstance(v, float) and np.isnan(v)) else (float(v) if isinstance(v, (np.floating, float)) else v))
            for k, v in d.items()}


AUTO_RETRIES = 1   # a failed run is resumed automatically once; after that it alerts and waits for a manager
SKIPPABLE_ON_RESUME = {"ingest", "identity", "persist_scores"}   # durable results already in the DB
BRIEF_BATCH = 50


class Run:
    """Bookkeeping for one run: steps, heartbeat, retries, resume."""

    def __init__(self, tenant_id: str, as_of: date, trigger: str, run_id=None, prior_steps=None,
                 attempts: int = 1, model_version: str | None = None):
        self.tenant_id, self.as_of, self.trigger = tenant_id, as_of, trigger
        self.run_id = run_id or uuid.uuid4()
        prior = [s for s in (prior_steps or []) if s.get("status") == "ok"]
        self.done = {s["step"] for s in prior}
        self.steps: list[dict] = prior
        self.stats: dict = {}
        self.attempts = attempts
        self.model_version = model_version          # pinned on resume, so scores stay consistent
        self.t0 = time.time()

    ORDER = ["ingest", "identity", "load_model", "load_data", "score", "reload_scores", "persist_scores", "briefs"]

    def _set_step(self, entry: dict):
        steps = [s for s in self.steps if s["step"] != entry["step"]] + [entry]
        self.steps = sorted(steps, key=lambda s: self.ORDER.index(s["step"]) if s["step"] in self.ORDER else 99)

    def step(self, name, fn, skippable: bool = False):
        if skippable and name in self.done:
            log.info("resume: skipping completed step %s", name)
            for s in self.steps:
                if s["step"] == name:
                    s["reused_on_attempt"] = self.attempts
            return None
        with tracer.start_as_current_span(f"agent.step.{name}") as span:
            span.set_attribute("triage.step", name)
            t0 = time.time()
            try:
                res = fn()
            except Exception as e:
                self._fail(name, t0, e, span)
                raise
            secs = round(time.time() - t0, 2)
            self._set_step({"step": name, "status": "ok", "seconds": secs})
            record("step_duration", secs, tenant=self.tenant_id, step=name, status="ok")
            self.beat()
            return res

    def _fail(self, name, t0, e, span):
        secs = round(time.time() - t0, 2)
        self._set_step({"step": name, "status": "failed", "seconds": secs,
                        "error": f"{type(e).__name__}: {e}"[:500]})
        record("step_duration", secs, tenant=self.tenant_id, step=name, status="failed")
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
        try:
            self.beat()
        except Exception:
            pass

    def beat(self):
        """Liveness + progress, written after every step and every brief batch."""
        with tenant_tx(self.tenant_id) as conn:
            conn.execute("UPDATE agent_runs SET heartbeat_at = now(), steps = %s, stats = %s WHERE run_id = %s",
                         (json.dumps(self.steps), json.dumps(self.stats, default=str), self.run_id))

    def save(self, status: str, error: str | None = None, model_version: str | None = None):
        with tenant_tx(self.tenant_id) as conn:
            conn.execute("""UPDATE agent_runs SET status=%s, finished_at = CASE WHEN %s <> 'running' THEN now() END,
                              heartbeat_at = now(), steps=%s, stats=%s, error=%s,
                              model_version=COALESCE(%s, model_version)
                            WHERE run_id=%s""",
                         (status, status, json.dumps(self.steps), json.dumps(self.stats, default=str), error,
                          model_version, self.run_id))


def _abandon_dead_runs(conn, tenant_id: str, reason: str) -> list[dict]:
    rows = conn.execute("""UPDATE agent_runs SET status = 'abandoned', finished_at = now(),
                              error = COALESCE(error, %s)
                            WHERE status = 'running' RETURNING run_id, as_of, heartbeat_at""", (reason,)).fetchall()
    for r in rows:
        alerts.send("Agent run abandoned", {"tenant": tenant_id, "run_id": r["run_id"], "as_of": r["as_of"],
                                            "last_heartbeat": r["heartbeat_at"], "reason": reason})
    return rows


def run_tenant(tenant_id: str, as_of: date, trigger: str = "manual", resume_run_id=None) -> dict:
    """One attempt. With ``resume_run_id``, continues that run from its first unfinished step."""
    with pool().connection() as lock_conn:
        got = lock_conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (f"triage:{tenant_id}",)).fetchone()["ok"]
        if not got:
            return {"tenant": tenant_id, "status": "skipped", "reason": "another run holds the lock"}
        try:
            with tenant_tx(tenant_id) as conn:
                # We hold the tenant lock, so any run still marked 'running' is a dead process.
                _abandon_dead_runs(conn, tenant_id, "process stopped before finishing (found by the next run)")
                if resume_run_id:
                    prev = conn.execute("SELECT * FROM agent_runs WHERE run_id = %s", (resume_run_id,)).fetchone()
                    if not prev:
                        return {"tenant": tenant_id, "status": "error", "reason": "run not found"}
                    if prev["status"] not in ("failed", "abandoned"):
                        return {"tenant": tenant_id, "status": "error",
                                "reason": f"only failed or abandoned runs can be resumed (this one is {prev['status']})"}
                    run = Run(tenant_id, prev["as_of"], prev["trigger"], prev["run_id"], prev["steps"],
                              prev["attempts"] + 1, prev["model_version"])
                    conn.execute("""UPDATE agent_runs SET status='running', attempts=%s, last_resumed_at=now(),
                                      heartbeat_at=now(), finished_at=NULL, error=NULL WHERE run_id=%s""",
                                 (run.attempts, run.run_id))
                else:
                    run = Run(tenant_id, as_of, trigger)
                    conn.execute("""INSERT INTO agent_runs (run_id, tenant_id, as_of, trigger, status, heartbeat_at)
                                    VALUES (%s,%s,%s,%s,'running', now())""", (run.run_id, tenant_id, as_of, trigger))
            run_id_var.set(str(run.run_id))
            tenant_var.set(tenant_id)
            with tracer.start_as_current_span("agent.run", attributes={
                    "tenant.id": tenant_id, "triage.run_id": str(run.run_id), "triage.as_of": str(run.as_of),
                    "triage.trigger": run.trigger, "triage.attempt": run.attempts}) as span:
                try:
                    _execute(run)
                    status = "partial" if run.stats.get("brief_failures") else "succeeded"
                    run.save(status, model_version=run.stats.get("model_version"))
                except Exception as e:
                    log.exception("run failed for %s", tenant_id)
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
                    status = "failed"
                    run.save("failed", error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}",
                             model_version=run.stats.get("model_version"))
                span.set_attribute("triage.status", status)
            record("runs", 1, tenant=tenant_id, status=status, trigger=run.trigger)
            record("run_duration", round(time.time() - run.t0, 1), tenant=tenant_id, status=status)
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"triage:{tenant_id}",))
    out = {"tenant": tenant_id, "run_id": str(run.run_id), "status": status, "attempts": run.attempts,
           "stats": run.stats, "steps": run.steps}
    if status == "failed":
        out["error"] = next((s.get("error") for s in run.steps if s["status"] == "failed"), None)
    return out


def _execute(run: Run):
    t, as_of = run.tenant_id, run.as_of
    ing = run.step("ingest", lambda: ingest_tenant(t, as_of, run.run_id), skippable=True)
    if ing is not None:
        run.stats["ingest"] = ing

    def _identity():
        with tenant_tx(t) as conn:
            return resolve_identities(conn)
    ident = run.step("identity", _identity, skippable=True)
    if ident is not None:
        run.stats["identity"] = {"changed": ident["changed"],
                                 "unresolved": sum(r["n"] for r in ident["by_method"] if r["method"] == "unresolved")}

    model = run.step("load_model", lambda: load_active_model(run.model_version))
    run.stats["model_version"] = model.version
    run.model_version = model.version

    def _load():
        with tenant_tx(t) as conn:
            fr = load_frames(conn, t)
            fr.ticket_subjects = {r["ticket_id"]: r["subject"] for r in
                                  conn.execute("SELECT ticket_id, subject FROM tickets")}
            tenant = conn.execute("SELECT * FROM tenants").fetchone()
            users = {r["crm_user_id"]: r["name"] for r in conn.execute("SELECT crm_user_id, name FROM crm_users")}
            prev = conn.execute("""SELECT run_id, as_of FROM agent_runs WHERE status IN ('succeeded','partial')
                                   AND as_of < %s ORDER BY as_of DESC, started_at DESC LIMIT 1""", (as_of,)).fetchone()
            prev_scores, prev_feats = {}, {}
            if prev:
                for r in conn.execute("SELECT * FROM account_scores WHERE run_id=%s", (prev["run_id"],)):
                    prev_scores[r["account_id"]] = {**r, "as_of": prev["as_of"].isoformat()}
                for r in conn.execute("SELECT account_id, features FROM feature_snapshots WHERE as_of=%s AND feature_version=%s",
                                      (prev["as_of"], FEATURE_VERSION)):
                    prev_feats[r["account_id"]] = r["features"]
        return fr, tenant, users, prev_scores, prev_feats
    fr, tenant, users, prev_scores, prev_feats = run.step("load_data", _load)

    if "persist_scores" in run.done:
        # Resume: scores for this run are already stored; reuse them instead of recomputing.
        def _reload():
            acc = accounts_as_of(fr, as_of)
            with tenant_tx(t) as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM account_scores WHERE run_id=%s", (run.run_id,))]
            return acc, rows
        acc, rows = run.step("reload_scores", _reload)
        run.step("persist_scores", lambda: None, skippable=True)
    else:
        def _score():
            acc = accounts_as_of(fr, as_of)
            acc = acc[acc["active"]]
            X = snapshot(fr, as_of, acc)
            p = model.predict(X, t)
            drivers = top_drivers(model, X)
            rows = []
            for i, acct in enumerate(X.index):
                cov = coverage(X.loc[acct])
                a = acc.loc[acct]
                rows.append(dict(account_id=acct, owner_id=a["owner_id"], p_churn=float(p[i]),
                                 priority=priority(float(p[i]), float(a["arr"] or 0), int(a["days_to_renewal"])),
                                 days_to_renewal=int(a["days_to_renewal"]), arr=float(a["arr"] or 0),
                                 insufficient_signal=cov["insufficient"],
                                 drivers={**drivers[acct], "coverage": cov,
                                          "renewal": {"date": str(a["next_renewal"]), "source": a["renewal_source"]}}))
            by_owner: dict = {}
            for r in rows:
                by_owner.setdefault(r["owner_id"], []).append(r)
            for book in by_owner.values():
                assign_bands(book, tenant["csm_weekly_capacity"], model.watch_min_p)
            return acc, X, rows
        acc, X, rows = run.step("score", _score)

        feats_by_acct = {a: _jsonable(X.loc[a].to_dict()) for a in X.index}
        for r in rows:
            r["changes"] = compute_changes(prev_scores.get(r["account_id"]), prev_feats.get(r["account_id"]),
                                           r, feats_by_acct[r["account_id"]])

        def _persist_scores():
            with tenant_tx(t) as conn:
                conn.execute("DELETE FROM account_scores WHERE run_id = %s", (run.run_id,))
                upsert(conn, "feature_snapshots",
                       [dict(tenant_id=t, account_id=a, as_of=as_of, feature_version=FEATURE_VERSION,
                             features=json.dumps(f)) for a, f in feats_by_acct.items()],
                       "ON CONFLICT (tenant_id, account_id, as_of, feature_version) DO UPDATE SET features = EXCLUDED.features")
                upsert(conn, "account_scores",
                       [dict(tenant_id=t, run_id=run.run_id, account_id=r["account_id"], as_of=as_of, owner_id=r["owner_id"],
                             model_version=model.version, p_churn=r["p_churn"], priority=r["priority"], band=r["band"],
                             rank_in_book=r["rank_in_book"], days_to_renewal=r["days_to_renewal"], arr=r["arr"],
                             insufficient_signal=r["insufficient_signal"], drivers=json.dumps(r["drivers"], default=str),
                             changes=json.dumps(r["changes"])) for r in rows], "")
        run.step("persist_scores", _persist_scores)

    bands = pd.Series([r["band"] for r in rows]).value_counts().to_dict()
    run.stats.update(accounts_scored=len(rows), bands=bands,
                     newly_on_call_list=sum(1 for r in rows if r["band"] == "call"
                                            and (prev_scores.get(r["account_id"]) or {}).get("band") != "call"))

    # ------------------------------------------------------------ briefs
    call = sorted([r for r in rows if r["band"] == "call"], key=lambda r: -r["priority"])
    call = call[: settings.llm_max_accounts_per_run]
    writer = get_writer()
    template = TemplateWriter()

    def _brief(r):
        a = acc.loc[r["account_id"]]
        info = dict(account_id=r["account_id"], name=a["name"], segment=a["segment"], arr=float(a["arr"] or 0),
                    next_renewal=str(a["next_renewal"]), days_to_renewal=int(a["days_to_renewal"]))
        score = dict(p_churn=r["p_churn"], rank_in_book=r["rank_in_book"], run_id=str(run.run_id))
        facts = grounding.build_facts(info, score, r["drivers"], [c for c in r["changes"] if c["kind"] != "first_seen"],
                                      recent_events(fr, r["account_id"], as_of))
        csm = users.get(r["owner_id"], "Your CSM")
        meta, gen, failure = {}, writer.name, None
        try:
            raw = writer.write(info, csm, facts)
            meta = raw.pop("_meta", {})
        except Exception as e:
            failure = f"{type(e).__name__}: {e}"
            raw, gen = template.write(info, csm, facts), template.name
        out, dropped = grounding.validate(raw, facts)
        # Anything the LLM failed to support is backfilled from the template, never invented.
        if not out["summary"] or not out["talking_points"] or not out["email"]:
            tpl, _ = grounding.validate(template.write(info, csm, facts), facts)
            out["summary"] = out["summary"] or tpl["summary"]
            out["talking_points"] = out["talking_points"] or tpl["talking_points"]
            out["email"] = out["email"] or tpl["email"] or {
                "subject": f"Checking in, {info['name']}",
                "body": f"Hi,\n\nCould we find time for a quick call soon to catch up?\n\nBest,\n{csm.split()[0]}"}
            if gen != template.name:
                gen = f"{gen}+template"
        to = _primary_contact(fr, r["account_id"], as_of)
        record("briefs", 1, tenant=t, generator=gen)
        if dropped:
            record("claims_removed", len(dropped), tenant=t)
        return r, info, facts, out, dropped, gen, {**meta, "error": failure} if failure else meta, to

    def _persist_batch(results):
        with tenant_tx(t) as conn:
            recently_approved = {r["account_id"] for r in conn.execute(
                "SELECT DISTINCT account_id FROM outreach_drafts WHERE status='approved' AND decided_at > now() - interval '14 days'")}
            briefs, drafts = [], []
            for r, info, facts, out, dropped, gen, meta, to in results:
                briefs.append(dict(tenant_id=t, run_id=run.run_id, account_id=r["account_id"], generator=gen,
                                   summary=out["summary"], talking_points=json.dumps(out["talking_points"]),
                                   facts=json.dumps(facts, default=str), dropped_claims=json.dumps(dropped),
                                   meta=json.dumps(meta, default=str)))
                if r["account_id"] in recently_approved:
                    continue
                drafts.append(dict(draft_id=uuid.uuid4(), tenant_id=t, run_id=run.run_id, account_id=r["account_id"],
                                   to_email=to, subject=out["email"]["subject"], body=out["email"]["body"]))
            ids = [d["account_id"] for d in drafts]
            if ids:
                conn.execute("UPDATE outreach_drafts SET status='superseded' WHERE status='pending' AND account_id = ANY(%s)", (ids,))
            upsert(conn, "briefs", briefs, "ON CONFLICT (run_id, account_id) DO NOTHING")
            upsert(conn, "outreach_drafts", drafts, "")

    def _briefs():
        # Written and saved in batches: a crash loses at most one batch, and a resume
        # skips every account that already has a brief for this run.
        with tenant_tx(t) as conn:
            have = {r["account_id"] for r in conn.execute("SELECT account_id FROM briefs WHERE run_id=%s", (run.run_id,))}
        todo = [r for r in call if r["account_id"] not in have]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i in range(0, len(todo), BRIEF_BATCH):
                chunk = todo[i:i + BRIEF_BATCH]
                # copy_context per task carries the trace + run/tenant ids into worker threads
                futures = [ex.submit(contextvars.copy_context().run, _brief, r) for r in chunk]
                _persist_batch([f.result() for f in futures])
                run.stats["briefs_written"] = len(have) + i + len(chunk)
                run.beat()
        return {"reused": len(have), "written": len(todo)}
    progress = run.step("briefs", _briefs)

    with tenant_tx(t) as conn:
        agg = conn.execute("""SELECT count(*) AS n, count(*) FILTER (WHERE meta ? 'error') AS failures,
                                     COALESCE(sum(jsonb_array_length(dropped_claims)), 0) AS dropped
                              FROM briefs WHERE run_id=%s""", (run.run_id,)).fetchone()
        gens = {r["generator"]: r["n"] for r in conn.execute(
            "SELECT generator, count(*) AS n FROM briefs WHERE run_id=%s GROUP BY 1", (run.run_id,))}
        nd = conn.execute("SELECT count(*) AS n FROM outreach_drafts WHERE run_id=%s", (run.run_id,)).fetchone()["n"]
    run.stats.update(briefs=agg["n"], drafts=nd, brief_generators=gens, brief_failures=agg["failures"],
                     dropped_claims=int(agg["dropped"]), briefs_reused_on_resume=(progress or {}).get("reused", 0))


def _primary_contact(fr, account_id: str, as_of: date) -> str | None:
    T = pd.Timestamp(as_of).tz_localize("UTC")
    c = fr.contacts[(fr.contacts["account_id"] == account_id) & (fr.contacts["arrived_at"] <= T)]
    c = c.sort_values("modified_at").groupby("contact_id").tail(1)
    champs = c[c["is_champion"]]
    pick = champs if not champs.empty else c
    return pick["email"].iloc[-1] if not pick.empty else None


def run_all(as_of: date, tenant: str | None = None, trigger: str = "manual", auto_retry: bool = True) -> list[dict]:
    """Run every tenant (isolated). A failed run is resumed automatically ONCE from its
    failed step; if it still fails (or ends partial) an alert is sent and a manager can
    resume it from the Agent runs page."""
    tenants = [x for x in register_tenants() if not tenant or x == tenant]
    results = []
    for t in tenants:
        res = run_tenant(t, as_of, trigger)
        if res["status"] == "failed" and auto_retry:
            log.warning("run %s failed; one automatic retry in %ss", t, settings.retry_backoff_s)
            time.sleep(settings.retry_backoff_s)
            res = run_tenant(t, as_of, trigger, resume_run_id=res["run_id"])
        if res["status"] in ("failed", "partial"):
            alerts.send(f"Agent run {res['status']}", {"tenant": t, "as_of": as_of, "run_id": res.get("run_id"),
                                                       "attempts": res.get("attempts"), "error": res.get("error")})
        log.info("run %s %s: %s", t, as_of, res["status"])
        results.append(res)
    return results


def resume_run(run_id: str) -> dict:
    with system_tx() as conn:
        r = conn.execute("SELECT tenant_id, as_of, trigger FROM agent_runs WHERE run_id=%s", (run_id,)).fetchone()
    if not r:
        return {"status": "error", "reason": "run not found"}
    res = run_tenant(r["tenant_id"], r["as_of"], r["trigger"], resume_run_id=run_id)
    if res["status"] in ("failed", "partial"):
        alerts.send(f"Resumed agent run {res['status']}", {"tenant": r["tenant_id"], "run_id": run_id,
                                                           "error": res.get("error")})
    return res


def watchdog(as_of: date, stale_minutes: int = 15, start_missing: bool = True) -> list[dict]:
    """Morning safety net: abandon stuck runs, and make sure every tenant has today's list."""
    report = []
    for t in register_tenants():
        with tenant_tx(t) as conn:
            stuck = conn.execute("""UPDATE agent_runs SET status='abandoned', finished_at=now(),
                                      error=COALESCE(error, 'no heartbeat for over ' || %s || ' minutes')
                                    WHERE status='running' AND heartbeat_at < now() - make_interval(mins => %s)
                                    RETURNING run_id""", (stale_minutes, stale_minutes)).fetchall()
            ok = conn.execute("""SELECT 1 FROM agent_runs WHERE as_of=%s AND status IN ('succeeded','partial') LIMIT 1""",
                              (as_of,)).fetchone()
        for s in stuck:
            alerts.send("Agent run abandoned (no heartbeat)", {"tenant": t, "run_id": s["run_id"]})
        entry = {"tenant": t, "abandoned": [str(s["run_id"]) for s in stuck], "has_run_for_today": bool(ok)}
        if not ok:
            alerts.send("No successful agent run for today", {"tenant": t, "as_of": as_of,
                                                              "action": "starting one now" if start_missing else "none"})
            if start_missing:
                entry["started"] = run_all(as_of, tenant=t, trigger="watchdog")[0]["status"]
        report.append(entry)
    return report
