-- factory-dashboard — operational read-model schema (readmodel.db)
-- Source of record: factory-dashboard-system-design-2026-07-08.md §4 (every table rebuildable),
-- as amended by the 2026-07-11 dated addenda (BINDING) and the capability-verification note.
--
-- This is the OPERATOR store. Each configured client tenant gets its OWN reduced store
-- (ledger_client_{tenant}.db) built from schema_client.sql — separate files, separate feeders
-- (design §4 / arch §4: isolation is structural, not a WHERE clause). The reduced client DDL is
-- therefore NOT created here; it lives in schema_client.sql (ux §9.1 lists both as S1 deliverables).
--
-- S1 (D0) ships the DDL + the founding seeds (norms, plan_milestones is EMPTY — its mirror is S4's).
-- The projector (S2+) is the SOLE writer of this DB; all web-layer opens are URI mode=ro (design §7/M-D4).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- §4.0 — tenant registry (config-sourced from tenants.yaml, mirrored here for joins).
-- 'operator' is reserved and has no client store.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_slug   TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    nats_account  TEXT,
    subject_prefix TEXT,
    projects_json TEXT NOT NULL DEFAULT '[]',
    store_path    TEXT
);

-- §7 — users -> tenant binding. Tenant is RE-READ from users.tenant_slug per request by
-- authenticated username, never trusted from the cookie payload (F-2f). credential_hash is a
-- PBKDF2-HMAC-SHA256 digest (see backend/auth.py); v1 skeleton auth, hardening deferred.
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    tenant_slug     TEXT NOT NULL REFERENCES tenants(tenant_slug),
    credential_hash TEXT NOT NULL
);

-- §4.1 — consumer watermarks (resume + SSE Last-Event-ID). Populated by the projector (S2).
CREATE TABLE IF NOT EXISTS consumer_watermarks (
    stream          TEXT NOT NULL,
    consumer        TEXT NOT NULL,
    last_stream_seq INTEGER NOT NULL DEFAULT 0,
    last_event_at   TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (stream, consumer)
);

-- §4.2 — agents roster (P1). Dual-source: fleet.* subjects AND $KV.agent-registry.> re-puts
-- (capability note drift 3). source_kind keeps register-only / kv-only / heartbeating honest.
CREATE TABLE IF NOT EXISTS agents (
    agent_id          TEXT PRIMARY KEY,
    name              TEXT,
    status            TEXT,
    queue_depth       INTEGER,
    active_tasks      INTEGER,
    uptime_seconds    INTEGER,
    last_heartbeat_at TEXT,
    deregistered_at   TEXT,
    source_kind       TEXT CHECK (source_kind IN ('fleet','kv','register_only')),
    manifest_json     TEXT
);

-- §4.3 — builds (P2). source keeps bootstrap-vs-live honest. Task counters are bus-only fields
-- (BuildComplete); forge_sqlite rows carry them as NULL -> rendered unmeasured (drift 6).
CREATE TABLE IF NOT EXISTS builds (
    build_id        TEXT PRIMARY KEY,
    feature_id      TEXT NOT NULL,
    project         TEXT,
    repo            TEXT,
    branch          TEXT,
    mode            TEXT,
    status          TEXT,
    queued_at       TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    pr_url          TEXT,
    merge_sha       TEXT,               -- amended DDR-DASH-001 (2026-07-11): merge receipt, primary
    tasks_total     INTEGER,
    tasks_completed INTEGER,
    tasks_failed    INTEGER,
    wave_total      INTEGER,
    current_wave    INTEGER,
    progress_pct    REAL,
    duration_seconds INTEGER,
    failure_reason  TEXT,
    correlation_id  TEXT,               -- nullable at the envelope level — guard it (drift/§3)
    title           TEXT,               -- no title feed exists v1 (drift 9 / A-9); nullable
    source          TEXT NOT NULL CHECK (source IN ('bus','forge_sqlite','both'))
);

-- §4.4 — stage & gate events (P3). coach_score is operator-view only (DF-008): never leaves here.
-- GATED derives from StageComplete.gate_mode + stage_log (no stage-gated producer — drift 5).
CREATE TABLE IF NOT EXISTS stage_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id      TEXT,
    feature_id    TEXT,
    stage_label   TEXT,
    target_kind   TEXT,
    status        TEXT,
    gate_mode     TEXT,
    coach_score   REAL,
    duration_secs INTEGER,
    completed_at  TEXT,
    origin        TEXT
);

-- §4.5 — approvals (P4). Keyed on request_id (slot_id is vestigial/unfed — drift 7).
-- decision enum: approve|reject|defer|override.
CREATE TABLE IF NOT EXISTS approvals (
    request_id       TEXT PRIMARY KEY,
    agent_id         TEXT,
    slot_id          TEXT,              -- vestigial; nullable, unfed (drift 7)
    action_description TEXT,
    risk_level       TEXT,
    requested_at     TEXT,
    timeout_seconds  INTEGER,
    decision         TEXT CHECK (decision IN ('approve','reject','defer','override')),
    decided_by       TEXT,
    decided_at       TEXT,
    latency_seconds  INTEGER,
    state            TEXT CHECK (state IN ('open','decided','timed_out'))
);

-- §4.6 — issue register (Q3). Derived by projection rules (§1 Q3), never by a model.
CREATE TABLE IF NOT EXISTS issues (
    issue_id   TEXT PRIMARY KEY,
    scope_type TEXT,
    scope_id   TEXT,
    kind       TEXT CHECK (kind IN ('escalation','gate_rejected','approval_waiting',
                                    'stalled','build_failed','seam_finding')),
    opened_at  TEXT,
    closed_at  TEXT,
    detail     TEXT,
    source_ref TEXT
);

-- §4.7 — planning runs mirror (P5). Read-only periodic mirror of forge SQLite.
CREATE TABLE IF NOT EXISTS planning_mirror (
    correlation_id    TEXT PRIMARY KEY,
    state             TEXT,
    originating_user  TEXT,
    expected_approver TEXT,
    request_text      TEXT,
    target_repo       TEXT,
    queued_at         TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    handoff_branch    TEXT,
    handoff_path      TEXT,
    defer_count       INTEGER,
    escalated_at      TEXT,
    error             TEXT,
    mirrored_at       TEXT
);

-- §4.8 — delivery ledger (P7). One row PER BAR CLEARED (§5): graduation appends, never mutates.
-- merge_sha added by amended DDR-DASH-001 (2026-07-11): bar clears on merge_sha OR pr_url.
CREATE TABLE IF NOT EXISTS ledger (
    feature_id   TEXT NOT NULL,
    project      TEXT,
    tenant       TEXT,
    title        TEXT,
    bar          TEXT CHECK (bar IN ('merged_pr','deployed_live_verified')),
    delivered_at TEXT,
    pr_url       TEXT,
    merge_sha    TEXT,
    repo         TEXT,
    branch       TEXT,
    evidence_ref TEXT,
    PRIMARY KEY (feature_id, bar)
);

-- §4.9 — norms (Q2). Founding values (design §1 Q2 table); update by dated note only.
CREATE TABLE IF NOT EXISTS norms (
    metric    TEXT PRIMARY KEY,
    green     TEXT NOT NULL,
    amber     TEXT NOT NULL,
    red       TEXT NOT NULL,
    source    TEXT NOT NULL,
    pinned_at TEXT NOT NULL
);

-- §4.10 — back-half projections, shaped now / populated 📋 (deploys, live_verdicts, episodes).
CREATE TABLE IF NOT EXISTS deploys (
    deploy_id        TEXT PRIMARY KEY,
    feature_id       TEXT,
    env_id           TEXT,
    artifact_digest  TEXT,
    image_digests    TEXT,
    deploy_record_ref TEXT,
    failed_step      TEXT,
    status           TEXT,
    duration_seconds INTEGER,
    correlation_id   TEXT,
    event_at         TEXT
);
CREATE TABLE IF NOT EXISTS live_verdicts (
    verdict_id        TEXT PRIMARY KEY,
    feature_id        TEXT,
    verdict           TEXT,   -- pass|fail|instrument_fail|environment_fail
    gate_ids          TEXT,
    assertions_json   TEXT,
    evidence_index_ref TEXT,
    app_url           TEXT,
    attempt           INTEGER,
    run_id            TEXT,
    event_at          TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id  TEXT PRIMARY KEY,
    episode_type TEXT,        -- the six backward-edge types
    feature_id  TEXT,
    payload_json TEXT,
    recorded_at TEXT
);

-- §4.11 — gateway-side usage (cost). Interim: dashboard-own-calls slice only until IN-5.
CREATE TABLE IF NOT EXISTS usage_gateway (
    period            TEXT,
    key_alias         TEXT,
    project           TEXT,
    seat              TEXT,
    requests          INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_gbp          REAL,
    is_nominal        INTEGER,
    PRIMARY KEY (period, key_alias)
);

-- §4.12 — frontier-side usage (cost). 📋 ask A-4.
CREATE TABLE IF NOT EXISTS usage_frontier (
    feature_id    TEXT,
    task_id       TEXT,
    stage         TEXT,
    model         TEXT,
    provider      TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_gbp      REAL,
    source        TEXT CHECK (source IN ('b7_payload','build_event','operator_reported')),
    PRIMARY KEY (feature_id, task_id, stage)
);

-- §4.13 — cost rollups. frontier and local are SEPARATE columns, never summed (arch §8).
CREATE TABLE IF NOT EXISTS cost_rollups (
    scope_type       TEXT CHECK (scope_type IN ('project','feature','seat')),
    scope_id         TEXT,
    window           TEXT,
    frontier_gbp     REAL,
    local_tokens     INTEGER,
    local_nominal_gbp REAL,
    coverage_note    TEXT,
    PRIMARY KEY (scope_type, scope_id, window)
);

-- §4.14 — service health (P6). checked_at drives the as-of chip + the dot-demotion rule (§5.2).
CREATE TABLE IF NOT EXISTS service_health (
    service    TEXT PRIMARY KEY,
    status     TEXT,
    detail     TEXT,
    checked_at TEXT
);

-- ux §4.7 — plan registry mirror target. Mirrored from plans.yaml BY THE PROJECTOR (S4), the
-- same way tenants.yaml -> tenants (startup + on file change, never a web-request write).
-- SHIPS EMPTY at S1 — the DDL only. Deviation state is computed by the query layer, never stored.
CREATE TABLE IF NOT EXISTS plan_milestones (
    milestone_id  TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    title         TEXT NOT NULL,
    feature_ids_json TEXT NOT NULL DEFAULT '[]',
    project       TEXT,
    target_window TEXT,
    mirrored_at   TEXT
);

-- ============================================================================
-- SEED — norms founding values (design §1 Q2 table; source: gap analysis §2, 2026-07-07).
-- Acceptance item 4's [G] verdict is uncomputable without these. Update by dated note only.
-- ============================================================================
INSERT OR IGNORE INTO norms (metric, green, amber, red, source, pinned_at) VALUES
    ('runs',              '1',    '2-3',    '>3',                'gap-analysis-2026-07-07 §2', '2026-07-07'),
    ('turns_per_task',    '<=2',  '2-4',    '>4',                'gap-analysis-2026-07-07 §2', '2026-07-07'),
    ('sdk_ceiling_hits',  '0',    '-',      '>=1',               'gap-analysis-2026-07-07 §2', '2026-07-07'),
    ('gate_failed_gated', '0-1',  '2-3',    '>3',                'gap-analysis-2026-07-07 §2', '2026-07-07'),
    ('stage_dwell',       '<30m', '30-120m','>120m',             'gap-analysis-2026-07-07 §2', '2026-07-07'),
    ('approval_waiting',  '<1h',  '1-4h',   '>4h / TIMED_OUT',   'gap-analysis-2026-07-07 §2', '2026-07-07');
