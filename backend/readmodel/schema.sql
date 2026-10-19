-- factory-dashboard — the dashboard's own store (readmodel.db).
--
-- Since 17 October 2026 (design docs/architecture/live-data-cost-time-voice-design-2026-10-17.md)
-- Forge's build record is the single source of truth for features, builds, planning, approvals,
-- delivery and time. The feed (`python -m backend.projector`) copies the parts of it the pages need
-- into the forge_* tables below every 15 seconds, read-only, and nothing else writes them. The
-- store is disposable: delete it and the feed rebuilds it from Forge's record on its next pass.
--
-- Both containers apply this file at start (it is idempotent), so neither depends on the other
-- having started first. The web side opens the store read-only (URI mode=ro) on every request.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Tenant registry (from backend/config/tenants.yaml, mirrored here for joins).
-- 'operator' is reserved and has no client store.
CREATE TABLE IF NOT EXISTS tenants (
    tenant_slug   TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    nats_account  TEXT,
    subject_prefix TEXT,
    projects_json TEXT NOT NULL DEFAULT '[]',
    store_path    TEXT
);

-- Users -> tenant binding. The tenant is re-read per request by username, never trusted from the
-- cookie. credential_hash is a PBKDF2-HMAC-SHA256 digest (backend/auth.py).
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    tenant_slug     TEXT NOT NULL REFERENCES tenants(tenant_slug),
    credential_hash TEXT NOT NULL
);

-- Live-update change log: the feed appends one row per page section whose data changed; the
-- /events channel tails it by `seq` (notification only, no row data). The feed prunes rows older
-- than an hour; a client whose last id predates the kept rows refetches everything.
CREATE TABLE IF NOT EXISTS change_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    panel      TEXT NOT NULL,
    scope_keys TEXT,
    at         TEXT NOT NULL
);

-- Service checks (Fleet page) and the feed's own heartbeat (service='projector').
CREATE TABLE IF NOT EXISTS service_health (
    service    TEXT PRIMARY KEY,
    status     TEXT,
    detail     TEXT,
    checked_at TEXT
);

-- When each source was last read. source='forge' is Forge's build record. last_success_at drives
-- the stale-data banner; a failed attempt keeps the last success and records the error.
CREATE TABLE IF NOT EXISTS mirror_status (
    source          TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error      TEXT,
    content_hash    TEXT,
    read_count      INTEGER NOT NULL DEFAULT 0   -- successful reads so far; only ever rises
);

-- ---------------------------------------------------------------------------------------------
-- The copy of Forge's record. Column names are Forge's own. builds, planning runs, queue items
-- and missions change in place, so each pass replaces them; stage rows and planning events are
-- only ever added by Forge, so each pass adds the rows after the last id already copied.
-- ---------------------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS forge_builds (
    build_id          TEXT PRIMARY KEY,
    feature_id        TEXT NOT NULL,
    repo              TEXT,
    branch            TEXT,
    project           TEXT,
    status            TEXT,
    triggered_by      TEXT,
    originating_user  TEXT,
    correlation_id    TEXT,
    queued_at         TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    error             TEXT,
    terminal_class    TEXT,
    mission_id        TEXT,
    mission_role      TEXT,  -- 'feature', 'mission-merge' (a mission's own merge, not a feature), or NULL
    pending_approval_request_id TEXT   -- the build gate's card while the build is PAUSED
);
CREATE INDEX IF NOT EXISTS forge_builds_feature ON forge_builds(feature_id);
CREATE INDEX IF NOT EXISTS forge_builds_correlation ON forge_builds(correlation_id);

CREATE TABLE IF NOT EXISTS forge_stage_log (
    id                INTEGER PRIMARY KEY,
    build_id          TEXT NOT NULL,
    stage_label       TEXT,
    target_identifier TEXT,
    status            TEXT,
    gate_mode         TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    details_json      TEXT
);
CREATE INDEX IF NOT EXISTS forge_stage_log_build ON forge_stage_log(build_id);

CREATE TABLE IF NOT EXISTS forge_planning_runs (
    correlation_id    TEXT PRIMARY KEY,
    state             TEXT,
    originating_user  TEXT,
    expected_approver TEXT,
    request_text      TEXT,
    target_repo       TEXT,
    triggered_by      TEXT,
    defer_count       INTEGER,
    paused_at         TEXT,
    escalated_at      TEXT,
    queued_at         TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    error             TEXT,
    pending_approval_request_id TEXT,  -- the older assumptions checkpoint's card while PAUSED
    parent_request_id TEXT
);

CREATE TABLE IF NOT EXISTS forge_planning_events (
    id             INTEGER PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    stage_label    TEXT,
    status         TEXT,
    gate_mode      TEXT,
    actor_identity TEXT,
    details_json   TEXT,
    recorded_at    TEXT
);
CREATE INDEX IF NOT EXISTS forge_planning_events_run ON forge_planning_events(correlation_id);

CREATE TABLE IF NOT EXISTS forge_queue (
    id               INTEGER PRIMARY KEY,
    sentence         TEXT,
    target_repo      TEXT,
    kind             TEXT,
    status           TEXT,
    originating_user TEXT,
    correlation_id   TEXT,
    queued_at        TEXT,
    admitted_at      TEXT,
    closed_at        TEXT,
    closed_reason    TEXT
);
CREATE INDEX IF NOT EXISTS forge_queue_correlation ON forge_queue(correlation_id);

CREATE TABLE IF NOT EXISTS forge_missions (
    id             TEXT PRIMARY KEY,
    title          TEXT,
    target_repo    TEXT,
    status         TEXT,
    correlation_id TEXT,
    created_at     TEXT,
    closed_at      TEXT,
    closed_reason  TEXT,
    plan_request_id  TEXT,   -- the plan card's request id, once it was sent
    originating_user TEXT,
    approved_by      TEXT,   -- who approved the plan (Forge does not record who rejected one)
    approved_at      TEXT
);

-- A mission's plan: its features in Forge's order (forge_order is Forge's rowid) and what each needs.
CREATE TABLE IF NOT EXISTS forge_mission_features (
    forge_order INTEGER,
    mission_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    sentence    TEXT,
    no_sign_in  INTEGER,
    PRIMARY KEY (mission_id, key)
);

CREATE TABLE IF NOT EXISTS forge_mission_needs (
    forge_order INTEGER,
    mission_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    needs_key   TEXT NOT NULL,
    PRIMARY KEY (mission_id, key, needs_key)
);

-- ---------------------------------------------------------------------------------------------
-- Plan baseline (from plans.yaml, mirrored by the feed). Deviation is computed, never stored.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plan_milestones (
    milestone_id  TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    title         TEXT NOT NULL,
    feature_ids_json TEXT NOT NULL DEFAULT '[]',
    project       TEXT,
    target_window TEXT,
    mirrored_at   TEXT
);

-- ---------------------------------------------------------------------------------------------
-- Cost: a copy of LiteLLM's spend log (design of 17 October 2026, requirement 3). The feed reads it
-- through a SELECT-only database login every 60 seconds and keeps one summary row per model call,
-- under LiteLLM's own request_id, so reading a row twice changes nothing. Every call is kept.
-- (This replaces the July design's usage_gateway, usage_frontier and cost_rollups tables, which
-- nothing ever fed or read.)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spend_calls (
    request_id        TEXT PRIMARY KEY,   -- LiteLLM's unique id for the call
    started_at        TEXT NOT NULL,      -- UTC, fixed-width 'YYYY-MM-DDTHH:MM:SS.ffffffZ' so text order is time order
    ended_at          TEXT,
    model_name        TEXT,               -- LiteLLM's model name (model_group); it shows the role. Stored, not shown.
    provider_model    TEXT,               -- the model behind that name, which the price list is keyed on
    key_alias         TEXT,
    feature_tag       TEXT,               -- the call's x-feature-id (Forge's correlation id), or NULL
    status            TEXT,               -- 'success' | 'failure'
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    litellm_spend     REAL NOT NULL DEFAULT 0,
    cost_usd          REAL,               -- nominal cost; NULL when the call's model has no price
    cost_basis        TEXT NOT NULL,      -- 'litellm' | 'estimated' (priced here from tokens) | 'no tokens' | 'not priced'
    price_ref         TEXT                -- which price: 'qwen/qwen3.8-flash, OpenRouter 2026-10-17'
);
CREATE INDEX IF NOT EXISTS spend_calls_started ON spend_calls(started_at);
CREATE INDEX IF NOT EXISTS spend_calls_tag ON spend_calls(feature_tag, started_at);

-- When the spend log was last read, kept apart from Forge's mirror_status: one can be healthy while
-- the other is stale. last_success_at is set only by a pass that read through to the newest row.
CREATE TABLE IF NOT EXISTS cost_feed_status (
    source            TEXT PRIMARY KEY,   -- 'litellm'
    last_attempt_at   TEXT,
    last_success_at   TEXT,
    last_error        TEXT,               -- a plain sentence; never a connection string
    earliest_start    TEXT,               -- the oldest call held
    last_reconcile_at TEXT,
    reconcile_note    TEXT
);
