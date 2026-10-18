"""Integration tests against the configured database (needs ingested data).

Run after `make demo` (or ingest + train + run). Skipped if no data is loaded.
"""
import pytest

from triage.db import system_tx, tenant_tx


def _tenants():
    try:
        with system_tx() as conn:
            return [r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM tenants ORDER BY 1")]
    except Exception:
        return []


TENANTS = _tenants()
pytestmark = pytest.mark.skipif(len(TENANTS) < 2, reason="needs at least two tenants loaded")


def test_rls_isolates_tenants():
    a, b = TENANTS[0], TENANTS[1]
    with tenant_tx(a) as conn:
        assert conn.execute("SELECT count(DISTINCT tenant_id) AS n FROM accounts").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM accounts WHERE tenant_id = %s", (b,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM tickets WHERE tenant_id <> %s", (a,)).fetchone()["n"] == 0


def test_rls_blocks_cross_tenant_writes():
    a, b = TENANTS[0], TENANTS[1]
    with pytest.raises(Exception):
        with tenant_tx(a) as conn:
            conn.execute("INSERT INTO audit_log (tenant_id, actor, action) VALUES (%s, 'test', 'x')", (b,))


def test_no_tenant_set_sees_nothing():
    from triage.db import pool
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE triage_app")
            assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


def test_ingest_is_idempotent():
    from datetime import date
    from triage.ingest.loader import ingest_tenant
    st = ingest_tenant(TENANTS[0], date(2100, 1, 1))
    assert st.get("files", 0) == 0


def test_every_score_is_traceable():
    with tenant_tx(TENANTS[0]) as conn:
        orphans = conn.execute("""
            SELECT count(*) AS n FROM account_scores s
            LEFT JOIN feature_snapshots f ON f.account_id = s.account_id AND f.as_of = s.as_of
            WHERE f.account_id IS NULL""").fetchone()["n"]
        assert orphans == 0
        bad = conn.execute("""SELECT count(*) AS n FROM account_scores s JOIN agent_runs r USING (run_id)
                              WHERE s.model_version IS DISTINCT FROM r.model_version""").fetchone()["n"]
        assert bad == 0


def test_no_draft_is_ever_sent():
    with tenant_tx(TENANTS[0]) as conn:
        statuses = {r["status"] for r in conn.execute("SELECT DISTINCT status FROM outreach_drafts")}
    assert statuses <= {"pending", "approved", "rejected", "superseded"}


def test_only_managers_can_start_or_resume_runs():
    from fastapi.testclient import TestClient
    import triage.api.main as m
    with system_tx() as conn:
        csm = conn.execute("SELECT email FROM users WHERE role='csm' LIMIT 1").fetchone()["email"]
    with TestClient(m.app) as c:
        tok = c.post("/api/auth/login", json={"email": csm, "password": "triage-demo"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.post("/api/runs", json={}, headers=h).status_code == 403
        assert c.post("/api/runs/00000000-0000-0000-0000-000000000000/resume", headers=h).status_code == 403
