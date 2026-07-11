"""Read-model DB bootstrap + the mode=ro connection helper the web layer uses.

**Fence 4 / design §7 / M-D4:** ALL web-request DB opens are URI `mode=ro`. The projector is the
sole writer of the read model at runtime (S2+). At S1 there is no projector, so `init_db` performs
the one-time, startup-only (never a web-request) bootstrap: apply schema.sql, mirror the tenant
registry into the `tenants` table (config-is-data), and seed the skeleton dev users. Request paths
use `connect_ro` exclusively.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from backend.auth import make_credential_hash
from backend.config_loader import Tenant

SCHEMA_PATH = Path(__file__).resolve().parent / "readmodel" / "schema.sql"
CLIENT_SCHEMA_PATH = Path(__file__).resolve().parent / "readmodel" / "schema_client.sql"


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open a READ-ONLY (URI mode=ro) connection for a web request path. WAL-courtesy: caller
    closes it promptly; per-request, short transactions (design §4.7)."""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def init_db(db_path: Path, tenants: Iterable[Tenant]) -> None:
    """Startup-only bootstrap (NOT a request path): create the operational DB if absent, apply the
    schema (idempotent), mirror the tenant registry, and seed dev users. rw connection here only."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _apply_schema(conn, SCHEMA_PATH)
        _mirror_tenants(conn, tenants)
        _seed_dev_users(conn)
        conn.commit()
    finally:
        conn.close()


def init_client_db(db_path: Path) -> None:
    """Create a reduced client store from schema_client.sql (D3 wires the feeder; the DDL ships now)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _apply_schema(conn, CLIENT_SCHEMA_PATH)
        conn.commit()
    finally:
        conn.close()


def _mirror_tenants(conn: sqlite3.Connection, tenants: Iterable[Tenant]) -> None:
    for t in tenants:
        conn.execute(
            """INSERT INTO tenants (tenant_slug, display_name, nats_account, subject_prefix,
                                    projects_json, store_path)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_slug) DO UPDATE SET
                   display_name=excluded.display_name,
                   nats_account=excluded.nats_account,
                   subject_prefix=excluded.subject_prefix,
                   projects_json=excluded.projects_json,
                   store_path=excluded.store_path""",
            (
                t.tenant_slug,
                t.display_name,
                t.nats_account,
                t.subject_prefix,
                json.dumps(list(t.projects)),
                t.store_path,
            ),
        )


def _seed_dev_users(conn: sqlite3.Connection) -> None:
    """Seed one dev user per registered tenant: username == tenant_slug, password == tenant_slug.

    DEV-ONLY skeleton credentials (S1). Idempotent — never overwrites an existing user row.
    """
    rows = conn.execute("SELECT tenant_slug FROM tenants").fetchall()
    for (tenant_slug,) in rows:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (tenant_slug,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (username, tenant_slug, credential_hash) VALUES (?, ?, ?)",
                (tenant_slug, tenant_slug, make_credential_hash(str(tenant_slug))),
            )
