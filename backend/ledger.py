"""Delivery ledger — bootstrap from forge `builds` + build-complete consumer + period query (§5).

STAGE-OWNED: built at S3 (D2). Bar clears on a merge receipt (`merge_sha` OR `pr_url` — amended
DDR-DASH-001); a COMPLETE build with neither renders the "complete — merge unverified" named gap,
never a delivered row. The internal module/table name stays `ledger`; the UI name is "Delivered"
(the query layer is the seam where the names meet — §4.4 naming map).

**Write side, projector-owned only** (design §7 / M-D4 — the projector is the sole writer of
`readmodel.db`; the web layer opens `mode=ro`). Two write paths, both idempotent and keyed
`(feature_id, bar)`:

- `bootstrap_from_forge` — cold-start scan of forge SQLite `builds WHERE status='COMPLETE'`
  (join `stage_log` for gate context), opened URI `mode=ro`, WAL-courtesy (fence 4). Each COMPLETE
  build is mirrored into the read-model `builds` table (`source='forge_sqlite'`) so an unverified
  complete can render its named gap; a build WITH a receipt also gets a `ledger` row at `merged_pr`.
- `append_from_build_complete` — steady state: a bus `build_complete` with a receipt appends a
  `merged_pr` ledger row; upserts are idempotent so a replay/restart never double-counts (design §5).

Zero NATS, zero publish, zero JetStream here — this module transforms already-decoded envelopes and
already-opened SQLite handles (the projector owns the connections; fences 1/2 hold).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from backend.projector.forge_mirror import connect_forge_ro
from backend.projector.projections.base import Envelope, iso

# The v1 delivery bar (amended DDR-DASH-001). Graduation (`deployed_live_verified`) is B7/B8, 📋.
BAR_MERGED = "merged_pr"


def has_receipt(merge_sha: object, pr_url: object) -> bool:
    """A merge receipt is present when EITHER `merge_sha` or `pr_url` is a non-empty value
    (amended DDR-DASH-001, 2026-07-11: the bar is *merged*, evidenced by a merge receipt —
    `merge_sha` primary, `pr_url` an equally-valid alternate). Empty strings do not count."""
    return bool((merge_sha and str(merge_sha).strip()) or (pr_url and str(pr_url).strip()))


# --- cold-start bootstrap from forge SQLite ---------------------------------


def bootstrap_from_forge(dash: sqlite3.Connection, forge_path: object, now: datetime) -> set[str]:
    """Scan forge `builds WHERE status='COMPLETE'`, mirror each into the read-model `builds` table
    (`source='forge_sqlite'`), and append a `merged_pr` ledger row for every complete carrying a
    receipt. Opens the forge DB `mode=ro` and CLOSES it before returning (WAL-courtesy, fence 4).
    Returns the panel ids that changed (for the SSE change log). A missing forge DB is an honest
    no-op — the launch reality is a near-empty ledger (capability note drift 2)."""
    path = Path(str(forge_path))
    if not path.exists():
        return set()

    forge = connect_forge_ro(path)
    try:
        rows = forge.execute(
            """SELECT build_id, feature_id, project, repo, branch, completed_at, pr_url, correlation_id
                 FROM builds WHERE status='COMPLETE'"""
        ).fetchall()
    finally:
        forge.close()  # closed before any read-model write — never co-holds both stores

    panels: set[str] = set()
    for r in rows:
        build_id, feature_id, project, repo, branch, completed_at, pr_url, correlation_id = (
            r["build_id"], r["feature_id"], r["project"], r["repo"], r["branch"],
            r["completed_at"], r["pr_url"], r["correlation_id"],
        )
        if not feature_id:
            continue
        # forge `builds` has no merge_sha column (verified prod schema) and no title feed (drift 9).
        merge_sha = None
        _mirror_complete_build(dash, str(build_id), str(feature_id), project, repo, branch,
                               completed_at, pr_url, merge_sha, correlation_id)
        panels.add("p2")
        if has_receipt(merge_sha, pr_url):
            tenant = _tenant_for(dash, project, repo)
            _upsert_ledger_row(
                dash, feature_id=str(feature_id), bar=BAR_MERGED,
                project=project, tenant=tenant, title=None, delivered_at=completed_at,
                pr_url=pr_url, merge_sha=merge_sha, repo=repo, branch=branch, source="forge_sqlite",
            )
            panels.add("p7")
    return panels


def _mirror_complete_build(
    dash: sqlite3.Connection, build_id: str, feature_id: str, project: object, repo: object,
    branch: object, completed_at: object, pr_url: object, merge_sha: object, correlation_id: object,
) -> None:
    """Reflect a forge COMPLETE build into the read-model `builds` table so the Delivered page can
    render an unverified complete as its named gap (§4.4). A pre-existing bus row for the same
    build_id is upgraded to `source='both'` rather than clobbered; forge-only builds land as
    `source='forge_sqlite'` (task counters stay NULL — bus-only fields, drift 6)."""
    existing = dash.execute("SELECT source FROM builds WHERE build_id=?", (build_id,)).fetchone()
    if existing is None:
        dash.execute(
            """INSERT INTO builds (build_id, feature_id, project, repo, branch, status,
                   completed_at, pr_url, merge_sha, correlation_id, source)
               VALUES (?, ?, ?, ?, ?, 'COMPLETE', ?, ?, ?, ?, 'forge_sqlite')""",
            (build_id, feature_id, project, repo, branch, completed_at, pr_url, merge_sha,
             correlation_id),
        )
        return
    new_source = "both" if existing[0] == "bus" else existing[0]
    dash.execute(
        """UPDATE builds SET status='COMPLETE',
               completed_at=COALESCE(completed_at, ?),
               pr_url=COALESCE(pr_url, ?),
               merge_sha=COALESCE(merge_sha, ?),
               source=? WHERE build_id=?""",
        (completed_at, pr_url, merge_sha, new_source, build_id),
    )


# --- steady state: bus build_complete append --------------------------------


def append_from_build_complete(dash: sqlite3.Connection, env: Envelope, now: datetime) -> set[str]:
    """Append a `merged_pr` ledger row from a bus `build_complete` envelope IFF it carries a
    receipt (amended DDR-DASH-001). Idempotent upsert keyed `(feature_id, bar)` — a JetStream-less
    replay or a projector restart re-applies without double-counting (design §5 steady state).
    Returns {'p7'} when a ledger row was written, else the empty set (the named-gap complete is
    already reflected in `builds` by p2's projection)."""
    if env.event_type != "build_complete":
        return set()
    p = env.payload
    feature_id = str(p.get("feature_id") or "")
    if not feature_id:
        return set()
    pr_url = p.get("pr_url")
    merge_sha = p.get("merge_sha")  # not on today's wire (A-11); accepted for when it lands
    if not has_receipt(merge_sha, pr_url):
        return set()
    tenant = _tenant_for(dash, env.project, p.get("repo"))
    delivered_at = iso(env.timestamp)
    _upsert_ledger_row(
        dash, feature_id=feature_id, bar=BAR_MERGED, project=env.project, tenant=tenant,
        title=p.get("summary"), delivered_at=delivered_at, pr_url=pr_url, merge_sha=merge_sha,
        repo=p.get("repo"), branch=p.get("branch"), source="bus",
    )
    return {"p7"}


# --- idempotent upsert keyed (feature_id, bar) ------------------------------


def _upsert_ledger_row(
    dash: sqlite3.Connection, *, feature_id: str, bar: str, project: object, tenant: object,
    title: object, delivered_at: object, pr_url: object, merge_sha: object, repo: object,
    branch: object, source: str,
) -> None:
    """One row PER BAR CLEARED (design §5: graduation appends, never mutates). Keyed
    `(feature_id, bar)` so restarts/replays are idempotent (design §5 steady state). The earliest
    `delivered_at` is preserved (a re-application never moves a delivery date forward); the change
    receipt fields are filled if a later pass learns them (COALESCE)."""
    existing = dash.execute(
        "SELECT delivered_at FROM ledger WHERE feature_id=? AND bar=?", (feature_id, bar)
    ).fetchone()
    if existing is None:
        dash.execute(
            """INSERT INTO ledger (feature_id, project, tenant, title, bar, delivered_at,
                   pr_url, merge_sha, repo, branch, evidence_ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feature_id, project, tenant, title, bar, delivered_at, pr_url, merge_sha, repo,
             branch, _evidence_ref(source, feature_id, delivered_at)),
        )
        return
    dash.execute(
        """UPDATE ledger SET
               project=COALESCE(project, ?), tenant=COALESCE(tenant, ?), title=COALESCE(title, ?),
               pr_url=COALESCE(pr_url, ?), merge_sha=COALESCE(merge_sha, ?),
               repo=COALESCE(repo, ?), branch=COALESCE(branch, ?)
           WHERE feature_id=? AND bar=?""",
        (project, tenant, title, pr_url, merge_sha, repo, branch, feature_id, bar),
    )


def _evidence_ref(source: str, feature_id: str, delivered_at: object) -> str:
    return f"{source}:{feature_id}@{delivered_at}"


def _tenant_for(dash: sqlite3.Connection, project: object, repo: object) -> str | None:
    """Best-effort project/repo → tenant mapping via the mirrored `tenants` registry (projects_json).
    The operator Delivered view is unfiltered, so a miss (None) is harmless; the binding matters when
    the client store (D3) is fed. 'operator' is reserved and never a delivery tenant."""
    needle = str(project) if project else (str(repo) if repo else "")
    if not needle:
        return None
    for slug, projects_json in dash.execute(
        "SELECT tenant_slug, projects_json FROM tenants WHERE tenant_slug != 'operator'"
    ).fetchall():
        try:
            projects = json.loads(projects_json) if projects_json else []
        except (ValueError, TypeError):
            projects = []
        if needle in projects:
            return str(slug)
    return None
