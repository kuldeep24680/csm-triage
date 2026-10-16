"""Database access.

Two ways in:
  * ``tenant_tx(tenant_id)``: the default. Switches to the restricted
    ``triage_app`` role and pins ``app.tenant_id`` for the transaction, so
    Postgres row-level security filters every statement. API handlers and
    per-tenant pipeline steps use this.
  * ``system_tx()``: owner role, no tenant filter. Only for migrations, login
    lookup and the orchestrator listing tenants.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None
_TENANT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=10,
            kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"}, open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def system_tx() -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def tenant_tx(tenant_id: str) -> Iterator[psycopg.Connection]:
    if not _TENANT_RE.match(tenant_id):
        raise ValueError(f"invalid tenant id: {tenant_id!r}")
    with pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE triage_app")
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn


MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate() -> list[str]:
    applied: list[str] = []
    with psycopg.connect(settings.database_url, autocommit=False) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.commit()
    return applied


def reset_schema() -> None:
    """Drop everything (dev only)."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
