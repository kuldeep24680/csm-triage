"""Minimal auth: email + password -> short-lived JWT carrying user, tenant and role,
plus a rotating refresh token (httpOnly cookie) that renews it.

The tenant in the token is the only tenant a request can ever touch: every
handler opens ``tenant_tx(user.tenant_id)``, and Postgres RLS does the rest.
SSO is out of scope for v1.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..db import system_tx

_bearer = HTTPBearer(auto_error=False)
log = logging.getLogger("triage.auth")


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2_sha256${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)).split("$")[2], digest)


@dataclass
class User:
    user_id: str
    tenant_id: str
    email: str
    name: str
    role: str
    crm_user_id: str | None


def issue_token(u: dict) -> str:
    """Short-lived access token (default 15 min). Stateless: verified by signature only."""
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": u["user_id"], "tenant": u["tenant_id"], "role": u["role"], "crm": u["crm_user_id"],
                       "name": u["name"], "email": u["email"], "iat": now,
                       "exp": now + timedelta(minutes=settings.access_token_ttl_minutes)},
                      settings.jwt_secret, algorithm="HS256")


# ---------------------------------------------------------------- refresh tokens
# Random opaque value, sent only as an httpOnly cookie; only its sha256 is stored.
# Rotated on every use; reuse of a rotated token revokes the whole sign-in (family).
REFRESH_COOKIE = "triage_refresh"
USER_FIELDS = ("user_id", "tenant_id", "email", "name", "role", "crm_user_id")


def _sha(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_refresh(conn, u: dict, family_id: str) -> str:
    raw = secrets.token_urlsafe(32)
    conn.execute("""INSERT INTO refresh_tokens (token_hash, family_id, user_id, tenant_id, expires_at)
                    VALUES (%s, %s, %s, %s, now() + make_interval(hours => %s))""",
                 (_sha(raw), family_id, u["user_id"], u["tenant_id"], settings.refresh_token_ttl_hours))
    return raw


def _session(conn, u: dict, family_id: str) -> dict:
    return {"token": issue_token(u), "expires_in": settings.access_token_ttl_minutes * 60,
            "refresh_token": _new_refresh(conn, u, family_id),
            "user": {k: u[k] for k in USER_FIELDS}}


def refresh(raw: str | None) -> dict:
    """Swap a valid refresh token for a new access token + a new refresh token."""
    if not raw:
        raise HTTPException(401, "no session")
    out, fail = None, None
    with system_tx() as conn:
        # FOR UPDATE: two tabs refreshing at once can't both rotate the same token.
        row = conn.execute("""SELECT r.*, (r.expires_at < now()) AS expired FROM refresh_tokens r
                              WHERE token_hash = %s FOR UPDATE""", (_sha(raw),)).fetchone()
        u = row and conn.execute("SELECT * FROM users WHERE user_id = %s", (row["user_id"],)).fetchone()
        if not row:
            fail = "invalid session"
        elif row["revoked_at"] is not None:
            # A rotated token came back: someone else has a copy. Kill the whole sign-in
            # (committed before the 401 is raised).
            conn.execute("UPDATE refresh_tokens SET revoked_at = coalesce(revoked_at, now()) WHERE family_id = %s",
                         (row["family_id"],))
            log.warning("refresh token reuse detected; sign-in %s revoked", row["family_id"])
            fail = "session revoked"
        elif row["expired"]:
            fail = "session expired"
        elif not u:   # re-read the user: deletion or a role change applies at the next refresh
            fail = "user no longer exists"
        else:
            out = _session(conn, u, str(row["family_id"]))
            conn.execute("UPDATE refresh_tokens SET revoked_at = now(), replaced_by = %s WHERE token_hash = %s",
                         (_sha(out["refresh_token"]), row["token_hash"]))
    if fail:
        raise HTTPException(401, fail)
    return out


def logout(raw: str | None) -> None:
    """Revoke the whole sign-in this refresh token belongs to (idempotent)."""
    if not raw:
        return
    with system_tx() as conn:
        conn.execute("""UPDATE refresh_tokens SET revoked_at = coalesce(revoked_at, now())
                        WHERE family_id = (SELECT family_id FROM refresh_tokens WHERE token_hash = %s)""",
                     (_sha(raw),))


# Brute-force guard: too many failed logins for one email or from one client -> 429 for a while.
# In-memory is enough for the single API process this runs as.
# Per email 10 failures; per client IP 50 (an office shares one IP).
LIMITS, WINDOW_S = {"email": 10, "ip": 50}, 15 * 60
_failures: dict[str, list[float]] = {}


def _throttled(keys: list[str]) -> bool:
    import time
    now = time.time()
    for k in keys:
        _failures[k] = [t for t in _failures.get(k, []) if now - t < WINDOW_S]
    return any(len(_failures[k]) >= LIMITS[k.split(":", 1)[0]] for k in keys)


def _record_failure(keys: list[str]) -> None:
    import time
    for k in keys:
        _failures.setdefault(k, []).append(time.time())


def login(email: str, password: str, client_ip: str | None = None) -> dict:
    email = email.lower().strip()
    keys = [f"email:{email}"] + ([f"ip:{client_ip}"] if client_ip else [])
    if _throttled(keys):
        raise HTTPException(429, "too many failed sign-in attempts; try again in 15 minutes")
    # Login is the one place we look a user up before knowing their tenant.
    with system_tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if not u or not verify_password(password, u["password_hash"]):
            _record_failure(keys)
            raise HTTPException(401, "invalid email or password")
        _failures.pop(f"email:{email}", None)
        # Housekeeping: drop this user's long-dead sessions.
        conn.execute("DELETE FROM refresh_tokens WHERE user_id = %s AND expires_at < now() - interval '1 day'",
                     (u["user_id"],))
        return _session(conn, u, str(uuid.uuid4()))


def current_user(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    if not cred:
        raise HTTPException(401, "missing bearer token")
    try:
        c = jwt.decode(cred.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    return User(c["sub"], c["tenant"], c["email"], c["name"], c["role"], c.get("crm"))


def seed_users(password: str) -> list[dict]:
    """One login per CRM user (CSM) + one manager per tenant. Idempotent."""
    created = []
    pw = hash_password(password)
    with system_tx() as conn:
        tenants = conn.execute("SELECT tenant_id, name FROM tenants").fetchall()
        for t in tenants:
            mgr = f"manager@{t['tenant_id']}.example"
            conn.execute("""INSERT INTO users (user_id, tenant_id, email, name, role, crm_user_id, password_hash)
                            VALUES (%s,%s,%s,%s,'manager',NULL,%s) ON CONFLICT (email) DO NOTHING""",
                         (str(uuid.uuid4()), t["tenant_id"], mgr, f"{t['name']} CS Manager", pw))
            created.append({"tenant": t["tenant_id"], "email": mgr, "role": "manager"})
            for u in conn.execute("SELECT crm_user_id, name, email FROM crm_users WHERE tenant_id=%s ORDER BY name",
                                  (t["tenant_id"],)):
                conn.execute("""INSERT INTO users (user_id, tenant_id, email, name, role, crm_user_id, password_hash)
                                VALUES (%s,%s,%s,%s,'csm',%s,%s) ON CONFLICT (email) DO NOTHING""",
                             (str(uuid.uuid4()), t["tenant_id"], u["email"], u["name"], u["crm_user_id"], pw))
                created.append({"tenant": t["tenant_id"], "email": u["email"], "role": "csm"})
    return created
