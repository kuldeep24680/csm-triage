"""Landing zone -> raw_records -> normalised tables.

Idempotency, at three levels:
  1. a landing file is loaded once (landing_files primary key);
  2. an exact duplicate record (same canonical payload) is stored once
     (raw_records unique record_hash), counted as a duplicate;
  3. a semantic duplicate (same message_id / ticket id / Stripe event id,
     different envelope) collapses on the normalised table's natural key.

Point-in-time: every normalised row keeps ``arrived_at`` = the landing file's
arrival date. Feature builders filter on it, so a model trained "as of"
March only sees what had actually arrived by March, late data included.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time as _time
from collections import defaultdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import settings
from ..db import system_tx, tenant_tx
from . import normalize as N

log = logging.getLogger(__name__)
SOURCE_ORDER = ["salesforce", "stripe", "zendesk", "product", "gmail"]


def landing_root() -> Path:
    return settings.data_dir / "landing"


def register_tenants() -> list[str]:
    """Create tenant rows for every tenant present in the landing zone."""
    ids = []
    for meta_path in sorted(landing_root().glob("*/_tenant.json")):
        meta = json.loads(meta_path.read_text())
        with system_tx() as conn:
            conn.execute(
                """INSERT INTO tenants (tenant_id, name, timezone, csm_weekly_capacity)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (tenant_id) DO UPDATE SET name = EXCLUDED.name,
                     timezone = EXCLUDED.timezone, csm_weekly_capacity = EXCLUDED.csm_weekly_capacity""",
                (meta["tenant_id"], meta["name"], meta["timezone"], meta["capacity"]))
        ids.append(meta["tenant_id"])
    return ids


def pending_files(conn, tenant_id: str, up_to: date) -> list[tuple[date, str, Path]]:
    done = {(r["source"], r["file_name"]) for r in
            conn.execute("SELECT source, file_name FROM landing_files").fetchall()}
    out = []
    root = landing_root() / tenant_id
    for source in SOURCE_ORDER:
        for p in sorted((root / source).glob("*.jsonl")):
            d = date.fromisoformat(p.stem)
            if d <= up_to and (source, p.name) not in done:
                out.append((d, source, p))
    out.sort(key=lambda x: (x[0], SOURCE_ORDER.index(x[1])))
    return out


# ------------------------------------------------------------- bulk helpers
_tmp_seq = 0


def _copy_into_temp(conn, table: str, cols: list[str], rows: list[tuple]) -> str:
    global _tmp_seq
    _tmp_seq += 1
    tmp = f"stg_{table}_{_tmp_seq}"
    conn.execute(f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {', '.join(cols)} FROM {table} WITH NO DATA")
    with conn.cursor().copy(f"COPY {tmp} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    return tmp


def upsert(conn, table: str, rows: list[dict], conflict: str) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    tmp = _copy_into_temp(conn, table, cols, [tuple(r[c] for c in cols) for r in rows])
    cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {tmp} {conflict}")
    return cur.rowcount


# --------------------------------------------------------------- the loader
def _merge(rows: list[dict], key: tuple[str, ...], prefer_existing: tuple[str, ...] = ()) -> list[dict]:
    """Collapse rows with the same key inside one batch (later non-null wins)."""
    out: dict = {}
    for r in rows:
        k = tuple(r[c] for c in key)
        if k not in out:
            out[k] = dict(r)
        else:
            cur = out[k]
            for c, v in r.items():
                if v is not None and (c not in prefer_existing or cur.get(c) is None):
                    cur[c] = v
    return list(out.values())


def _merge_tickets(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["arrived_at"]):
        t = out.get(r["ticket_id"])
        if t is None:
            t = out[r["ticket_id"]] = dict(
                tenant_id=r["tenant_id"], ticket_id=r["ticket_id"], org_id=r["org_id"],
                requester_email=r["requester_email"], subject=r["subject"], category=r["category"],
                priority=r["priority"], created_at=r["created_at"], created_arrived_at=r["arrived_at"],
                solved_at=None, solved_arrived_at=None, csat=None)
        if r["solved_at"] and t["solved_at"] is None:
            t["solved_at"], t["solved_arrived_at"] = r["solved_at"], r["arrived_at"]
        if r["csat"]:
            t["csat"] = r["csat"]
    return list(out.values())


def load_chunk(conn, tenant_id: str, files: list[tuple[date, str, Path]], ctx: dict, run_id=None) -> dict:
    raw_rows, rejected = [], []
    norm: dict[str, list[dict]] = defaultdict(list)
    per_file: dict[str, dict] = {}
    for arrival, source, path in files:
        arrived_at = N.arrival_ts(arrival)
        parse = N.PARSERS[source]
        n = 0
        with open(path) as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    rec = json.loads(line)
                    rows = parse(rec, ctx)
                except (json.JSONDecodeError, N.Reject, KeyError, ValueError, TypeError) as e:
                    rejected.append(dict(tenant_id=tenant_id, source=source, file_name=path.name,
                                         line_no=line_no, reason=f"{type(e).__name__}: {e}"[:300],
                                         payload=line[:2000]))
                    continue
                canon = json.dumps(rec, sort_keys=True, separators=(",", ":"))
                raw_rows.append(dict(tenant_id=tenant_id, source=source, file_name=path.name,
                                     record_hash=hashlib.sha256(canon.encode()).hexdigest(),
                                     source_record_id=N.source_record_id(source, rec),
                                     arrived_at=arrived_at, payload=canon))
                for table, row in rows:
                    row["tenant_id"] = tenant_id
                    norm[table].append((arrived_at, row))
        per_file[(source, path.name)] = dict(arrival=arrival, records=n, inserted=0, rejected=0)
    for r in rejected:
        per_file[(r["source"], r["file_name"])]["rejected"] += 1

    # raw layer (exact duplicates dropped here)
    if raw_rows:
        cols = list(raw_rows[0].keys())
        tmp = _copy_into_temp(conn, "raw_records", cols, [tuple(r[c] for c in cols) for r in raw_rows])
        cur = conn.execute(
            f"INSERT INTO raw_records ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {tmp} "
            f"ON CONFLICT (tenant_id, source, record_hash) DO NOTHING RETURNING source, file_name")
        for r in cur.fetchall():
            per_file[(r["source"], r["file_name"])]["inserted"] += 1
    upsert(conn, "rejected_records", rejected, "")

    # normalised layer; earliest arrival wins for append-only tables
    def rows_of(table, with_arrival=True):
        out = []
        for arrived_at, row in sorted(norm.get(table, []), key=lambda x: x[0]):
            row = dict(row)
            if with_arrival:
                row["arrived_at"] = arrived_at
            out.append(row)
        return out

    upsert(conn, "crm_users", _merge(rows_of("crm_users", False), ("crm_user_id",)),
           "ON CONFLICT (tenant_id, crm_user_id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email")
    for t, pk in (("crm_account_versions", "account_id, modified_at"),
                  ("crm_contacts", "contact_id, modified_at"),
                  ("crm_opportunities", "opp_id, modified_at")):
        upsert(conn, t, rows_of(t), f"ON CONFLICT (tenant_id, {pk}) DO NOTHING")
    upsert(conn, "zendesk_orgs", _merge(rows_of("zendesk_orgs", False), ("org_id",)),
           "ON CONFLICT (tenant_id, org_id) DO UPDATE SET name = EXCLUDED.name, domains = EXCLUDED.domains")
    upsert(conn, "tickets", _merge_tickets(rows_of("tickets")), """
        ON CONFLICT (tenant_id, ticket_id) DO UPDATE SET
          created_arrived_at = LEAST(tickets.created_arrived_at, EXCLUDED.created_arrived_at),
          org_id = COALESCE(tickets.org_id, EXCLUDED.org_id),
          solved_at = COALESCE(tickets.solved_at, EXCLUDED.solved_at),
          solved_arrived_at = CASE WHEN tickets.solved_at IS NULL THEN EXCLUDED.solved_arrived_at
                                   WHEN EXCLUDED.solved_at IS NULL THEN tickets.solved_arrived_at
                                   ELSE LEAST(tickets.solved_arrived_at, EXCLUDED.solved_arrived_at) END,
          csat = COALESCE(EXCLUDED.csat, tickets.csat)""")
    upsert(conn, "emails", _merge(rows_of("emails"), ("tenant_id", "message_id"), ("arrived_at",)),
           "ON CONFLICT (tenant_id, message_id) DO NOTHING")
    upsert(conn, "usage_weekly", _merge(rows_of("usage_weekly"), ("tenant_id", "workspace_id", "week_start"), ("arrived_at",)),
           "ON CONFLICT (tenant_id, workspace_id, week_start) DO NOTHING")
    upsert(conn, "product_workspaces", _merge(rows_of("product_workspaces", False), ("workspace_id",)), """
        ON CONFLICT (tenant_id, workspace_id) DO UPDATE SET
          workspace_name = COALESCE(EXCLUDED.workspace_name, product_workspaces.workspace_name),
          account_hint = COALESCE(EXCLUDED.account_hint, product_workspaces.account_hint)""")
    upsert(conn, "stripe_customers", _merge(rows_of("stripe_customers", False), ("customer_id",)),
           "ON CONFLICT (tenant_id, customer_id) DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email, "
           "account_hint = COALESCE(EXCLUDED.account_hint, stripe_customers.account_hint)")
    upsert(conn, "billing_events", _merge(rows_of("billing_events"), ("tenant_id", "event_id"), ("arrived_at",)),
           "ON CONFLICT (tenant_id, event_id) DO NOTHING")

    landing = [dict(tenant_id=tenant_id, source=s, file_name=fn, arrival_date=v["arrival"], records=v["records"],
                    inserted=v["inserted"], duplicates=v["records"] - v["inserted"] - v["rejected"],
                    rejected=v["rejected"], run_id=run_id)
               for (s, fn), v in per_file.items()]
    upsert(conn, "landing_files", landing, "")
    return dict(files=len(files), records=sum(v["records"] for v in per_file.values()),
                inserted=sum(v["inserted"] for v in per_file.values()),
                rejected=len(rejected),
                duplicates=sum(x["duplicates"] for x in landing))


def ingest_tenant(tenant_id: str, up_to: date, run_id=None, chunk_days: int = 45) -> dict:
    t0 = _time.time()
    with tenant_tx(tenant_id) as conn:
        tz = conn.execute("SELECT timezone FROM tenants").fetchone()["timezone"]
        files = pending_files(conn, tenant_id, up_to)
    ctx = {"tz": ZoneInfo(tz), "own_domain": f"{tenant_id}.example"}
    totals = defaultdict(int)
    i = 0
    while i < len(files):
        first = files[i][0]
        j = i
        while j < len(files) and (files[j][0] - first).days < chunk_days:
            j += 1
        with tenant_tx(tenant_id) as conn:
            st = load_chunk(conn, tenant_id, files[i:j], ctx, run_id)
        for k, v in st.items():
            totals[k] += v
        i = j
    totals["seconds"] = round(_time.time() - t0, 1)
    return dict(totals)
