"""Sign-in session: 15-minute access token, rotating refresh cookie, reuse detection, logout.

Needs a reachable database with migrations applied; creates (and removes) its own tenant and user.
"""
import uuid
from datetime import datetime, timezone

import jwt
import pytest

from triage.config import settings
from triage.db import system_tx


def _db_ok():
    try:
        with system_tx() as conn:
            conn.execute("SELECT 1 FROM refresh_tokens LIMIT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="needs a migrated database")

TENANT, EMAIL, PW = "authtest", f"csm-{uuid.uuid4().hex[:6]}@authtest.example", "pw-for-tests"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from triage.api.auth import hash_password
    from triage.api.main import app
    uid = str(uuid.uuid4())
    with system_tx() as conn:
        conn.execute("""INSERT INTO tenants (tenant_id, name, timezone, csm_weekly_capacity)
                        VALUES (%s, 'Auth Test', 'UTC', 15) ON CONFLICT DO NOTHING""", (TENANT,))
        conn.execute("""INSERT INTO users (user_id, tenant_id, email, name, role, crm_user_id, password_hash)
                        VALUES (%s, %s, %s, 'Test CSM', 'csm', NULL, %s)""", (uid, TENANT, EMAIL, hash_password(PW)))
    with TestClient(app) as c:
        yield c
    with system_tx() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (uid,))   # cascades to refresh_tokens


def _login(c):
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    assert r.status_code == 200, r.text
    return r


def test_access_token_lives_15_minutes(client):
    r = _login(client)
    body = r.json()
    assert "refresh_token" not in body and body["expires_in"] == 15 * 60
    c = jwt.decode(body["token"], settings.jwt_secret, algorithms=["HS256"])
    assert c["exp"] - c["iat"] == settings.access_token_ttl_minutes * 60 == 900


def test_refresh_cookie_is_httponly_and_scoped(client):
    sc = _login(client).headers["set-cookie"].lower()
    assert "triage_refresh=" in sc and "httponly" in sc and "samesite=strict" in sc and "path=/api/auth" in sc


def test_expired_access_token_is_rejected(client):
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t = jwt.encode({"sub": "x", "tenant": TENANT, "role": "csm", "name": "x", "email": EMAIL,
                    "iat": past, "exp": past}, settings.jwt_secret, algorithm="HS256")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_refresh_rotates_and_reuse_revokes_the_sign_in(client):
    _login(client)
    first = client.cookies.get("triage_refresh")
    r = client.post("/api/auth/refresh")
    assert r.status_code == 200 and r.json()["token"]
    second = client.cookies.get("triage_refresh")
    assert second and second != first
    # Replaying the old (rotated) cookie = theft: 401, and the current one dies too.
    client.cookies.clear()
    client.cookies.set("triage_refresh", first, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401
    client.cookies.clear()
    client.cookies.set("triage_refresh", second, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_logout_revokes_refresh(client):
    _login(client)
    raw = client.cookies.get("triage_refresh")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    client.cookies.set("triage_refresh", raw, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_only_the_hash_is_stored(client):
    _login(client)
    raw = client.cookies.get("triage_refresh")
    with system_tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM refresh_tokens WHERE token_hash = %s",
                            (raw,)).fetchone()["n"] == 0
