"""Minimal auth: email + password -> JWT carrying user, tenant and role.

The tenant in the token is the only tenant a request can ever touch: every
handler opens ``tenant_tx(user.tenant_id)``, and Postgres RLS does the rest.
SSO is out of scope for v1.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..db import system_tx

_bearer = HTTPBearer(auto_error=False)


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
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": u["user_id"], "tenant": u["tenant_id"], "role": u["role"], "crm": u["crm_user_id"],
                       "name": u["name"], "email": u["email"], "iat": now,
                       "exp": now + timedelta(hours=settings.jwt_ttl_hours)},
                      settings.jwt_secret, algorithm="HS256")


def login(email: str, password: str) -> dict:
    # Login is the one place we look a user up before knowing their tenant.
    with system_tx() as conn:
        u = conn.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),)).fetchone()
    if not u or not verify_password(password, u["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    return {"token": issue_token(u), "user": {k: u[k] for k in ("user_id", "tenant_id", "email", "name", "role", "crm_user_id")}}


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
