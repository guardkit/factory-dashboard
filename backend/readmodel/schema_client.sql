-- factory-dashboard — reduced CLIENT read-model schema (ledger_client_{tenant}.db)
-- Source of record: factory-dashboard-system-design-2026-07-08.md §4 (final paragraph) + arch §4.
--
-- One such DB PER configured client tenant (v1 instantiates one: ledger_client_finproxy.db).
-- Its OWN reduced DDL, NOT a copy of the operational shapes (gate finding F-2d):
--   * NO evidence_ref column exists in this store.
--   * `project` ranges ONLY over the tenant's configured repo set.
-- Written solely by that tenant's projector connection (D3+), from the A-8 reduced delivery
-- events (which carry project + per-build spend total at source). Per-project / per-tenant spend
-- are IN-STORE sums of per-build spend — no APPMILLA-derived row ever crosses app-side (F-2b).
--
-- The DF-008 firewall is enforced at the PRODUCER / account boundary; this store simply cannot
-- hold operator-only fields because they have no columns here. Client web-layer opens are mode=ro.

PRAGMA journal_mode = WAL;

-- Reduced delivery ledger — DF-008-permitted content only (no evidence_ref, no coach fields).
CREATE TABLE IF NOT EXISTS ledger_client (
    feature_id   TEXT NOT NULL,
    project      TEXT,               -- tenant's own configured repo set only
    title        TEXT,
    bar          TEXT CHECK (bar IN ('merged_pr','deployed_live_verified')),
    delivered_at TEXT,
    pr_url       TEXT,               -- merge receipt link (pr_url OR merge-commit change link)
    PRIMARY KEY (feature_id, bar)
);

-- Per-build spend (from the A-8 reduced event; forge computes the total at build close).
CREATE TABLE IF NOT EXISTS cost_build (
    feature_id            TEXT,
    project               TEXT,
    window                TEXT,
    spend_frontier_gbp    REAL,
    spend_local_nominal_gbp REAL,
    coverage_note         TEXT,
    PRIMARY KEY (feature_id, window)
);

-- Per-project spend — in-store sum across the project's builds.
CREATE TABLE IF NOT EXISTS cost_project (
    project               TEXT,
    window                TEXT,
    spend_frontier_gbp    REAL,
    spend_local_nominal_gbp REAL,
    coverage_note         TEXT,
    PRIMARY KEY (project, window)
);

-- Per-tenant total — in-store sum across the tenant's configured repo set.
CREATE TABLE IF NOT EXISTS cost_tenant (
    window                TEXT PRIMARY KEY,
    spend_frontier_gbp    REAL,
    spend_local_nominal_gbp REAL,
    coverage_note         TEXT
);
