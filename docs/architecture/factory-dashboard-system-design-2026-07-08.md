# Factory Dashboard — System Design (/system-design)

**Status:** v1 · 2026-07-08 · companion to
`factory-dashboard-system-arch-2026-07-08.md` (read that first — decisions ADR-DASH-001..007 are
binding here). Produced on the `/system-design` skill spine in kickoff-scripted form (contracts +
data model + DDRs; single-file house style in `docs/architecture/`, not the skill's
`docs/design/` split — deviation noted).
**Consumer:** the build-plan sessions (`build-plan-2026-07-08.md`); WS2 B7's author (via
`wire-consumer-requirements-2026-07-08.md`, which extracts §2's 📋 rows as field-level asks);
the in-session adversarial review (record at the end of the build plan).
**Verified inputs (all checked on disk 2026-07-08):** nats-core 0.5.0 payloads
(`src/nats_core/topics.py`, `events/_pipeline.py`, `_fleet.py`, `_agent.py`, `_jarvis.py`,
`_memory.py`) · nats-infrastructure `streams/stream-definitions.json` +
`config/accounts/accounts.conf.template` · forge `src/forge/lifecycle/schema*.sql`,
`planning/run_store.py`, `pipeline/stage_taxonomy.py` · guardkit `qa/formats/*` (b9f5eff8),
`orchestrator/instrumentation/schemas.py` · fleet-memory backward-edge contract @ `974669c` ·
dgx-spark DF-005 + LiteLLM runbook/config.

**Feed-status legend (used in every matrix row):**
✅ = contract exists AND a producer emits it today ·
🟡 = contract/durable record exists but the producer is inert, unverified-live, or partial ·
📋 = does not exist; gated on the named session (WS2 B7/B8, WS1-I/E, WS4-S7, or an infra ask
IN-n / wire ask A-n from the wire-requirements note).

---

## 1 · The status model — the three questions, made computable

Everything below serves three per-feature / per-project questions (arch §7):

**Q1 — How far through?** = position on the journey
`intake → planning → approval → spec/plan → build (waves/tasks) → merged+PR → deployed →
live-verified`, where the first six stations are computable today and the last two are 📋 B7/B8.
Progress within build = `BuildProgressPayload.overall_progress_pct` + wave/task counters.

**Q2 — On track?** = current run's shape vs the gap-§2 calibrated norms, pinned as data
(`norms` table, §4.9) with these founding values (source: gap analysis §2, 2026-07-07; update by
dated note only):
| Metric | Green | Amber | Red |
|---|---|---|---|
| runs (attempts) per feature | 1 | 2–3 | >3 (the June shape was 6–9) |
| turns per task (avg) | ≤2 | 2–4 | >4 |
| SDK ceiling hits | 0 | — | ≥1 |
| gate FAILED/GATED events per feature | 0–1 | 2–3 | >3 |
| stage dwell with no event | <30 min | 30–120 min | >120 min → "stalled" issue |
| approval waiting on named human | <1 h | 1–4 h (escalation window) | >4 h / TIMED_OUT |

**Measured-or-excluded rule (gate finding F-6, 2026-07-08):** every `on_track` signal carries
`measured: bool`. A signal whose feed is absent for this record (e.g. turns-per-task and
SDK-ceiling-hits, which have **no live feed until the A-4 loop-stats ask lands** — they live
today only in guardkit artifacts the dashboard does not scrape; or bus-only counters missing on
`source='forge_sqlite'` bootstrap rows) is **excluded from the verdict and echoed into
`coverage.gaps`** — never counted as zero, never a vacuous green. Division guards: missing/zero
denominators ⇒ unmeasured, not 0. Stalled-detection is additionally gated on projector
liveness: if the consumer watermark itself is stale, the panel reports "projection lagging
(since ⟨ts⟩)" instead of manufacturing stalled-run issues (F-5).

**Q3 — What are the issues?** = the issue register (§4.6): escalations, gate REJECTED,
approval-waiting-on-named-human (with age + who), stalled runs, build failures, seam findings
(📋 — seam findings become machine events only when WS2 F14 / WS3-S5 merge-review records or the
`spec_survival`/`build_outcome` episodes exist; today they live in review docs the dashboard
cannot and does not parse — stated honestly in the worked example, §10).

---

## 2 · Event → projection matrix (per panel)

### Panels live NOW (build Phase 1 against these)

| # | Panel | Feeds (payload → projection) | Status |
|---|---|---|---|
| P1 | **Fleet health / agent roster** | `fleet.register` (AgentManifest) → `agents`; `fleet.heartbeat.{agent_id}` (AgentHeartbeatPayload: status, queue_depth, active_tasks, uptime_seconds) → `agents` upsert; `fleet.deregister` → mark offline. FLEET stream is 1h/limits — live-only by design, no history claimed | ✅ |
| P2 | **Work in flight / build board** | `pipeline.build-queued.{feat}` (BuildQueuedPayload) → `builds` insert ✅ live-proven; `build-started/progress/paused/resumed/complete/failed/cancelled` → `builds` update. Contracts all exist (nats-core `_pipeline.py:148-748`); forge's publisher + bridge-registry code exists but live emission is unverified for the non-queued family — Phase 1 verifies and records. **PIPELINE consumption rule (gate finding F-12, BLOCKER-fixed):** PIPELINE is a workqueue stream — until IN-1 resolves, the projector touches `pipeline.>` via **core-NATS subscribe only**; it MUST NOT create, bind, or ack any JetStream consumer on PIPELINE, including under dev credentials (a JS consumer on a workqueue competes with forge's and can consume-delete its work items). Durable consumers are permitted only on limits-retention streams, named `dash-*`, never binding an existing durable | ✅/🟡 |
| P3 | **Stage & gate board** | `pipeline.stage-complete/.stage-gated` (StageComplete/StageGatedPayload: stage_label, status, gate_mode, coach_score, duration_secs) → `stage_events`; backfill/reconcile from forge SQLite `stage_log` (gate_mode, coach_score, threshold_applied — `schema.sql:66-91`) | 🟡 bus / ✅ SQLite |
| P4 | **Approvals & escalations** | `agents.approval.forge.*` (ApprovalRequestPayload) + `.response` (ApprovalResponsePayload: decision, decided_by) → `approvals`; open-request age drives the "waiting on named human" issue. Live-proven G1 2026-07-07 | ✅ |
| P5 | **Planning runs** | forge SQLite `planning_runs` + `planning_run_events` (schema_v3; states QUEUED…PLANNED_HANDOFF; actor_identity, gate records) → `planning_mirror`; `pipeline.planning-queued.*` (PlanningQueuedPayload) for live arrival. Front half is one operator session from live (gap §1a) — panel renders whatever exists; AGENTS-stream approval events for `plan-{cid}` slots per WS1-I item 2's convention | 🟡 (inert until MP-010/J04) |
| P6 | **Gateway / serving health** | HTTP polls, not bus — **pinned to load-neutral endpoints only** (gate finding F-15): LiteLLM `GET /v1/models`, llama-swap `GET /health` + `GET /v1/models`, NATS `:8222/healthz` → `service_health`. **LiteLLM's `/health` endpoint is explicitly forbidden on any poll path** — it fires real test completions per configured model and would reshuffle GPU residency twice a minute. Poll ≤1/30s | ✅ (available now) |
| P7 | **Delivery ledger v1** (Rich view + FinProxy view when the feed lands) | `pipeline.build-complete` (BuildCompletePayload: **pr_url**, tasks_completed/failed/total, duration_seconds, repo, branch) → `ledger` at bar `merged_pr`; bootstrap/reconcile from forge SQLite `builds` (status=COMPLETE, pr_url — `schema.sql:15-52`). FinProxy copy requires asks A-8 + IN-4 (arch §4) | 🟡 (Rich now; FinProxy 📋 feed) |

### Panels GATED on unrun producers (build later phases against the asks)

| # | Panel | Feeds | Gated on |
|---|---|---|---|
| P8 | **Deploy status** | `DeployQueued/Started/Complete/Failed` → `deploys` (env_id, artifact/image digests, deploy_record_ref, correlation_id, failed_step) | 📋 B7 (payloads) + B8 (producer) — asks A-1/A-2 |
| P9 | **QA / live-gate verdicts** | `QAVerdictPayload`/`LiveGateResultPayload` → `live_verdicts` (verdict incl. instrument_fail/environment_fail, gate_ids, assertions, evidence_index_ref, app_url, attempt) | 📋 B7 + B8 — ask A-3 |
| P10 | **Planning lifecycle timeline** (bus-fed; replaces SQLite-mirror polling) | `planning_started/complete/failed` + spec-ready handoff event (correlation_id + Mode-P-minted feat_id) | 📋 WS1-I items 1–3 — ask A-5 |
| P11 | **Flywheel / episode panels** (Rich only) | `memory.episode.>` (MemoryEpisodeV1; MEMORY stream 365d/limits — the one genuinely replayable stream) → `episodes`; the six backward-edge types (`planning_outcome, approval_decision, deploy_record, live_verdict, grading_outcome, spec_survival`) light up per-type as producers land | ✅ transport / 📋 producers (WS1-E build, WS2 B8, WS4-S4/S7) |
| P12 | **Cost panel** (arch §8) | Gateway side: LiteLLM spend table when the opt-in Postgres layer is enabled (key-per-project×seat convention) → `usage_gateway`; DB-less interim: the dashboard records `x-litellm-response-cost` for **its own chat calls only** (labelled as such). Frontier side: per-stage usage rollups on B7 payloads / BuildComplete extension → `usage_frontier` | 📋 asks A-4 (frontier) + IN-5 (gateway Postgres); interim slice ✅ |
| P13 | **Ledger graduated bar** | `live_verdict`-class events flip ledger rows to bar `deployed_live_verified` (§5 decision) | 📋 B7/B8 |

**DF-008 firewall on every FinProxy-visible row (re-checked per field):** the FinProxy store
receives ONLY: feature id, title/summary, project, delivered bar + date, PR URL, per-project and
per-build spend totals. It never receives: coach scores, turns, agent ids, heartbeats,
evidence paths, seat-level cost, gateway key names, planning internals, failure reasons.

### Per-chat-tool mapping (the same projections — no separate feed exists or ever will)

| Tool | Reads (Rich registry) | Status inherited from |
|---|---|---|
| `feature_status` | `features`, `builds`, `stage_events`, `approvals`, `issues`, (`deploys`, `live_verdicts` 📋) | P2–P4, P8–P9 |
| `project_rollup` | aggregates of the above per project + `ledger` | P2–P7 |
| `open_issues` | `issues` (+ ages, owners) | P3–P5 |
| `delivered_period` | `ledger` (tenant-bound) | P7/P13 |
| `cost_summary` | `cost_rollups` (Rich handler); the FinProxy handler reads `ledger_finproxy.cost_build`/`cost_project` ONLY — there is no "filtered view" of the operational store (gate finding F-2c) | P12 |

**The FinProxy registry is enumerated, not derived (gate finding F-2d):** it contains exactly
three tools — `delivered_period`, `project_rollup_lite` (delivered counts + per-project spend
from `ledger_finproxy` only), `cost_summary_lite` (per-project/per-build totals, no `by_seat`
in its schema) — and nothing else. `feature_status` and `open_issues` do not exist in the
FinProxy registry in v1 (their substance is operational). FinProxy tool-result objects are
DF-008-clean **at the tool boundary** (they are constructed only from `ledger_finproxy.db`
columns), so the degrade-to-table fallback is inherently safe for that audience (F-2e).

---

## 3 · Chat grounding-tool contracts

Registry-shaped (name → handler + JSON schema + tenant binding), so the Slack twin (arch §7.6)
is a transport adapter later. All tools are **pure reads of the read DB** — no NATS, no model
calls, no filesystem beyond resolving evidence links for display.

```
feature_status(feature_id: str) -> {
  feature_id, project, title,
  progress: {stage, stage_status, waves_done/total, tasks_done/failed/total, pct},
  on_track: {verdict: green|amber|red, signals: [{metric, value, norm, band}]},
  issues: [issue],                      # open issues scoped to the feature
  delivered: {bar, at, pr_url} | null,
  citations: [citation], coverage: {gaps: [str]}   # e.g. "no deploy events exist yet (WS2 B7/B8)"
}

project_rollup(project: str, window?: iso-interval) -> {
  project, features: [{feature_id, stage, on_track}], delivered_count, open_issue_count,
  spend: {frontier_gbp, local_tokens, local_nominal_gbp?},   # audience-filtered
  citations, coverage
}

open_issues(scope: {project?|feature_id?}) -> { issues: [...], citations, coverage }

delivered_period(tenant: str, window: iso-interval) -> {
  records: [{feature_id, title, bar, delivered_at, pr_url, spend?}], citations, coverage
}
# `tenant` is NOT a caller argument in the FinProxy registry — it is bound at registry
# construction; the FinProxy handler reads ledger_finproxy only. The Rich registry may pass any tenant.

cost_summary(scope: {project?|feature_id?}, window: iso-interval) -> {
  frontier_gbp, local_tokens, local_gpu_seconds?, local_nominal_gbp (labelled nominal),
  by_seat?: {...},          # Rich registry only — absent from the FinProxy registry's schema
  coverage: {captured_share, uninstrumented: [str]},   # honest: what spend is NOT captured
  citations
}
```

**Citation object:** `{kind: event|sqlite_row|episode|evidence, ref: str, at: iso-ts}` — e.g.
`{kind: "event", ref: "pipeline.build-complete.FEAT-3ED2 seq=1041", at: "2026-07-06T14:13:34Z"}`
or `{kind: "sqlite_row", ref: "forge builds/build-FEAT-3ED2-20260706125839"}`.

**Response-envelope fields every tool must include (gate findings F-5/F-10):**
`as_of: {watermark_ts, mirror_ts}` (data freshness — the renderer displays the lag whenever it
exceeds a threshold, so projector downtime is never presented as "now") and an echo of the
call's own `window`/scope arguments (so period/scope claims are citable, not model narrative).

**Grounding rules (binding; mechanized, not prompted-only). Hardened 2026-07-08 by the gate's
fabrication review — the original checker verified only that citations EXIST; these rules make
it verify that claims MATCH, are COMPLETE, and are FRESH:**
1. The model receives only the current user turn + this turn's tool results. Prior assistant
   prose is excluded from model input — every turn re-queries; stale verdicts cannot leak
   forward (F-11).
2. **Structured claims:** the NL answer is emitted as sentences/bullets each carrying ≥1
   citation token. A citation-free sentence is itself a grounding failure — zero-citation
   answers cannot vacuously pass (F-2).
3. **Entailment, not existence:** the grounding checker (deterministic, post-model) verifies
   (a) every cited ref appeared in this turn's tool results, AND (b) every claim-bearing scalar
   in the sentence — status enums, verdict colours, counts, dates, money — string-matches a
   field value inside the specific cited record (F-1).
4. **No model arithmetic:** any numeral not literally present in a tool result is rejected;
   tools pre-compute every aggregate they want displayed. Cross-currency totals can never be
   rendered because no tool ever returns one (frontier and local are separate columns end to
   end — F-3).
5. **No forecasts:** a deterministic claim-class filter rejects future-dated or
   modal-predictive sentences (ETAs, "should complete by") — no tool contract emits forecasts,
   so none may be rendered (F-4).
6. **Gaps are load-bearing:** every non-empty `coverage.gaps` entry from this turn's tool
   results must appear (string containment) in the rendered answer; dropping a gap is a
   grounding failure (F-8). Unanswerable ⇒ gaps surfaced verbatim — refusal is data-driven.
7. **Degrade to truth, labelled:** any failed check discards the NL answer and renders the raw
   tool results as tables under the header "Could not produce a grounded answer to:
   ⟨user question⟩ — raw results of ⟨tools called⟩ below" (F-9).
8. Tool loop: max 6 calls/turn; serving default `chat` alias, `workhorse` for multi-tool turns;
   `claude-*` attended opt-in only (arch §7.5, incl. the no-eviction rule during active
   builds). Chat requests carry the dashboard's own virtual key (`factory-dashboard--chat`) so
   the chat's own spend appears in its own cost panel.

---

## 4 · Read-model schema (SQLite v1; every table rebuildable)

One operational DB (`readmodel.db`) + one FinProxy DB (`ledger_finproxy.db`) — separate files,
separate feeders (arch §4). Sketch (columns abridged to the load-bearing):

1. `consumer_watermarks(stream, consumer, last_stream_seq, updated_at)` — resume + SSE
   Last-Event-ID.
2. `agents(agent_id PK, name, status, queue_depth, active_tasks, uptime_seconds,
   last_heartbeat_at, deregistered_at, manifest_json)`.
3. `builds(build_id PK, feature_id, project, repo, branch, mode, status, queued_at, started_at,
   completed_at, pr_url, tasks_total, tasks_completed, tasks_failed, wave_total, current_wave,
   progress_pct, duration_seconds, failure_reason, correlation_id, source
   CHECK(source IN ('bus','forge_sqlite','both')))` — `source` keeps bootstrap-vs-live honest.
4. `stage_events(id PK, build_id, feature_id, stage_label, target_kind, status, gate_mode,
   coach_score, duration_secs, completed_at, origin)` — coach_score never leaves the Rich view.
5. `approvals(request_id PK, agent_id, slot_id, action_description, risk_level, requested_at,
   timeout_seconds, decision, decided_by, decided_at, latency_seconds, state
   CHECK(state IN ('open','decided','timed_out')))`.
6. `issues(issue_id PK, scope_type, scope_id, kind CHECK(kind IN ('escalation','gate_rejected',
   'approval_waiting','stalled','build_failed','seam_finding')), opened_at, closed_at,
   detail, source_ref)` — derived by projection rules (§1 Q3), not by a model.
7. `planning_mirror(correlation_id PK, state, originating_user, expected_approver, request_text,
   target_repo, queued_at, started_at, completed_at, handoff_branch, handoff_path, defer_count,
   escalated_at, error, mirrored_at)` — read-only periodic mirror of forge SQLite
   (`FORGE_DB_PATH`, default `~/.forge/forge.db`). **WAL-courtesy discipline (gate finding
   F-16):** open with URI `mode=ro`, autocommit, per-query transactions <100 ms, connection
   closed between mirror passes — a long-lived read transaction would block forge's WAL
   checkpointing (the factory caring about the dashboard, which is forbidden). If forge's
   `-wal` file grows past a pinned threshold while the mirror runs, assumption A1 is failed and
   the periodic-copy fallback becomes mandatory.
8. `ledger(feature_id, project, tenant, title, bar CHECK(bar IN ('merged_pr',
   'deployed_live_verified')), delivered_at, pr_url, evidence_ref, PRIMARY KEY(feature_id, bar))`
   — one row PER BAR CLEARED (§5): graduation appends, never mutates.
9. `norms(metric PK, green, amber, red, source, pinned_at)` — §1 Q2 values.
10. `deploys`, `live_verdicts`, `episodes` — shaped now (mirroring the backward-edge contract §4
    field lists + B7 ask fields), populated 📋.
11. `usage_gateway(period, key_alias, project, seat, requests, prompt_tokens, completion_tokens,
    cost_gbp, is_nominal)` — from the LiteLLM spend table (key convention arch §8) when IN-5
    lands; interim: dashboard-own-calls slice only.
12. `usage_frontier(feature_id, task_id, stage, model, provider, input_tokens, output_tokens,
    cost_gbp, source CHECK(source IN ('b7_payload','build_event','operator_reported')))` — 📋
    ask A-4.
13. `cost_rollups(scope_type CHECK(scope_type IN ('project','feature','seat')), scope_id,
    window, frontier_gbp, local_tokens, local_nominal_gbp, coverage_note,
    PRIMARY KEY(scope_type, scope_id, window))` — **frontier and local are separate columns,
    never summed** (arch §8); `coverage_note` states what is not captured (attended sessions,
    pre-instrumentation builds).
14. `service_health(service PK, status, detail, checked_at)`.

`ledger_finproxy.db` gets its **own reduced DDL, not a copy of the operational shapes** (gate
finding F-2d): `ledger_client(feature_id, project, title, bar, delivered_at, pr_url)` — note
**no `evidence_ref` column exists in this store** — plus `cost_build(feature_id, project,
window, spend_frontier_gbp, spend_local_nominal_gbp, coverage_note)` and
`cost_project(project, window, …same columns…)`. Written solely by the connection-B projector,
from the A-8 **client-facing reduced delivery events** (which carry the per-build spend total
at source — see the re-cut A-8 in the wire note): per-project spend is the in-store sum of
per-build spend, so **no APPMILLA-derived row ever crosses app-side** (F-2b). Gateway-side
project spend (PO/chat seats) is out of FinProxy v1 scope and named in `coverage_note`.

---

## 5 · THE LEDGER SEMANTICS DECISION (named, dated — DDR-DASH-001)

**Decided 2026-07-08:**
- **v1: "delivered" = merged + PR.** Feed: `BuildCompletePayload` (carries `pr_url` today) +
  forge SQLite `builds` reconciliation. This is what the wire can honestly support now.
- **Graduation: "delivered" = deployed + live-verified**, when B7/B8 land — a ledger row at bar
  `deployed_live_verified` is appended when a counting live-gate `pass` verdict
  (instrument/environment re-runs never count — backward-edge §4.4 rule) closes over the
  feature.
- **Both views label which bar each record cleared, honestly**: FinProxy renders
  "Delivered — merged, PR ⟨link⟩" vs "Delivered — deployed & live-verified ⟨date⟩". Records
  never silently upgrade: graduation is a second, dated row (schema §4.8), and the period query
  reports the highest bar cleared *within the window* with the earlier bar's date preserved.
- **Why this is honest rather than embarrassing:** the WS2 DoD itself redefines factory output
  as "merged + QA-verified + deployed + live-gated" (WS2 plan §5) — the ledger's bar graduates
  exactly when the factory's own definition does, and the label says which regime a record was
  delivered under.

**Ledger bootstrap & replay (the D6-intent mechanism under real retention, ADR-DASH-001):**
- **Bootstrap** (cold start / rebuild): scan forge SQLite `builds` where `status='COMPLETE'`
  (join `stage_log` for gates), insert `ledger` rows at `merged_pr`; then JetStream replay of
  whatever remains within retention (AGENTS 24h; MEMORY 365d for episode-fed rows; PIPELINE
  only if IN-1 lands — today its workqueue retention means bus history is effectively
  forge-consumed).
- **Steady state:** durable push consumers append-only; watermarks make restarts idempotent
  (upserts keyed on build_id/feature_id+bar).
- **Period query:** `SELECT ... FROM ledger WHERE tenant=? AND delivered_at >= ? AND
  delivered_at < ? ORDER BY delivered_at` with the bar-labelling rule above — served identically
  to the panel and to `delivered_period`. **No double counting (gate finding F-10b):** a
  graduation row whose feature already cleared an earlier bar in a *prior* reported window is
  reported as an upgrade, not a new delivery — `delivered_count` counts features first
  delivered in the window, with upgrades listed separately.

---

## 6 · Push contract (SSE — ADR-DASH-003 consequence)

- `GET /events?panels=p1,p2,...` (session-scoped; FinProxy sessions can subscribe only to their
  view's channels). Server emits `event: panel_update`, `data: {panel, scope_keys, at}` —
  **notification-only, no payload data in the push**; the client re-fetches the panel fragment
  (HTMX `hx-get` on trigger). This keeps tenancy trivially safe: the push channel carries no
  row data, and the re-fetch goes through the same tenant-bound query layer as everything else.
- `id:` = read-DB change counter; reconnect with `Last-Event-ID` replays missed notifications
  cheaply (or degrades to full panel refresh).
- Chat streaming: `POST /chat` → SSE token stream for the NL turn; tool-call progress events
  (`event: tool_call`, name + args echo) so the user sees the grounding happen.

## 7 · Auth + tenant scoping (mechanics of arch §4)

Server-side sessions (signed cookie); `users(username, tenant CHECK(tenant IN
('operator','finproxy')), credential_hash)`. **Tenant is re-read from `users.tenant` by
authenticated username on every request — never trusted from the cookie payload** (gate finding
F-2f). Tenant selects: (a) which read DB the request's query layer opens (operator →
`readmodel.db`; finproxy → `ledger_finproxy.db` ONLY — the operational DB path is not present
in that request context), (b) which chat tool registry is constructed, (c) which SSE channels
are subscribable. **All web-layer DB opens use URI `mode=ro`** — the projector is the sole
writer of both stores, asserted by M-D4 (F-17). **The M-D2/ledger parity check is an offline
batch job with its own read-only credential — not a live request path**: no request context
ever co-holds handles to both stores (F-2g). NATS credentials live in the projector's
environment only; no request path touches NATS. Network: Tailscale, port-scoped share (arch
§5); no public listener in v1.

---

## 8 · Target file tree

```
factory-dashboard/
├── docs/architecture/            ← this doc set
├── backend/
│   ├── app.py                    ← FastAPI entrypoint; sessions; SSE
│   ├── projector/
│   │   ├── consumers.py          ← nats-core durable consumers (conn A + conn B)
│   │   ├── forge_mirror.py       ← read-only forge SQLite mirror
│   │   ├── health_polls.py       ← gateway/llama-swap/NATS HTTP polls
│   │   └── projections/          ← one module per matrix row (P1..P13)
│   ├── readmodel/
│   │   ├── schema.sql            ← §4 tables
│   │   └── queries.py            ← THE query layer (panels + chat tools share it)
│   ├── chat/
│   │   ├── registry.py           ← tool registry, tenant-bound construction
│   │   ├── tools.py              ← §3 contracts
│   │   ├── grounding.py          ← citation checker (deterministic)
│   │   └── llm.py                ← LiteLLM :4000 client (chat/workhorse aliases)
│   ├── ledger.py                 ← §5 bootstrap + period query
│   └── auth.py                   ← §7
├── frontend/
│   ├── templates/                ← Jinja panels, ledger, chat window
│   └── static/                   ← htmx.js, sse ext, stylesheet
└── tests/
```

---

## 9 · DDR index

| DDR | Decision (dated 2026-07-08) |
|---|---|
| DDR-DASH-001 | Ledger semantics: v1 merged+PR → graduated deployed+live-verified; bar labelled per record; graduation appends (§5) |
| DDR-DASH-002 | Push = SSE, notification-only (no row data on the push channel); re-fetch through the tenant-bound query layer (§6) |
| DDR-DASH-003 | Grounding checker is deterministic post-model verification of **entailment, completeness, and freshness** — per-sentence citations, scalar claim-matching against cited records, no model arithmetic, no forecasts, gaps rendered, `as_of` disclosed; failed grounding degrades to labelled raw tool-result tables (§3; hardened 2026-07-08 by the gate's fabrication review) |
| DDR-DASH-004 | `source`/`origin` columns keep bootstrap-vs-live provenance explicit; `coverage` fields keep cost/status gaps explicit — the dashboard never presents partial capture as total (§4) |

---

## 10 · WORKED EXAMPLE — FEAT-3ED2 (+ the DD4F catch) through this design

Ground truth (verified on disk in forge): `.guardkit/features/FEAT-3ED2.yaml` — Mode P Planning
Chain (FEAT-SPL-002), created 2026-07-06T11:29, execution 12:58:39→14:13:34 (≈74m55s), 11 tasks
/ 6 waves / 16 turns, 0 failures, TASK-MP-010 deferred (operator follow-up);
`.guardkit/autobuild/FEAT-3ED2/events.jsonl` (27 events: per-task player events with
turn_count, 6 router wave-close events); merge `34b17d0`; then FEAT-DD4F (TASK-MP-011 wiring
fix, 3 turns, 14:27→14:53, merge `1ad98c0`); then the post-merge review
(`docs/reviews/feat-spl-002-post-merge-review-2026-07-06.md`) caught the DD4F fix itself calling
all three planning functions with nonexistent kwargs, swallowed by the DDR-007 soft-fail — 16/16
findings confirmed, 0/32 refutations — spawning TASK-MP-012.

**The chat answer to "How's FEAT-3ED2 doing?" (as `feature_status` would render it), each line
citing projection + feed:**

> **Progress: DELIVERED (bar: merged + PR) — 100% of build.** 11/11 tasks across 6/6 waves;
> build ran 2026-07-06 12:58→14:13 (74m55s).
> *(`ledger` row @ bar merged_pr ← BuildCompletePayload `pipeline.build-complete.FEAT-3ED2`
> [pr_url, tasks 11/11, duration] 🟡 — for this run the build rode guardkit-on-forge outside the
> forge daemon, so the projection's actual source would have been the bootstrap path: forge
> SQLite `builds` row + `.guardkit` execution block; `builds.source='forge_sqlite'` says so
> honestly. Wave/task granularity ← BuildProgressPayload ✅-contract/🟡-producer, reconciled from
> guardkit events.jsonl-class instrumentation.)*
> No deploy or live-verification is recorded — deploy tracking does not exist yet (WS2 B7/B8).
> *(`coverage.gaps` ← absence of `deploys`/`live_verdicts` rows 📋.)*
>
> **On track: GREEN on the measured signals; two signals unmeasured; one re-work signal not yet
> machine-computable.** Measured: single run (attempts=1, norm green), 0 task failures
> (tasks_failed=0/11), gate events clean, 74m55s wall time. *(`norms` §1 vs `builds` counters ←
> BuildCompletePayload 🟡 + forge `stage_log` ✅; each signal carries `measured: true`.)*
> Unmeasured, excluded from the verdict and listed as gaps (§1 rule): turns-per-task and
> SDK-ceiling-hits — **no live feed exists for loop stats until the A-4 loop-stats ask lands**
> (for this pre-dashboard run they are recoverable one-off from guardkit's durable records —
> 16 turns/11 tasks, 0 ceiling hits — but that is archaeology, not a projection, and the tool
> reports it as a gap, not a number). *(F-6/F-7 honesty rule.)*
> A follow-up fix build (FEAT-DD4F, same afternoon) targeted this feature's wiring — but the
> re-work link is **not machine-computable in v1**: DD4F is a different feature_id, `builds`
> has no scope column, and "DD4F fixes 3ED2's PS-002 gap" exists only as prose in
> FEAT-DD4F.yaml. The tool surfaces it as a coverage gap ("re-work linkage lands with
> `spec_survival` episodes 📋 WS4-S7/WS1-E"), not as an amber verdict it cannot ground.
>
> **Issues: 1 open, 1 honest blind spot.**
> (1) *Operator follow-up outstanding:* TASK-MP-010 deferred — runtime validation required;
> waiting on the operator bundle. *(`issues` kind=approval_waiting/deferred ← today only in
> guardkit feature YAML + forge task tracker, which this dashboard does NOT scrape (arch §3.2);
> becomes machine-visible via the planning-lifecycle / spec-ready events carrying deferred-work
> refs 📋 ask A-5, or the `planning_outcome` episode 📋 WS1-E.)*
> (2) *The DD4F post-merge catch — the wiring fix that was itself broken, swallowed by
> soft-fail — would NOT appear in v1*: it was caught by an adversarial post-merge review whose
> record is a markdown doc, not an event. It surfaces when merge-review verdicts become
> machine records (WS2 F14 format + WS3-S5 gate 📋, and/or `build_outcome`/`spec_survival`
> episodes 📋) — **wire ask A-7 exists precisely because of this example.** Until then the
> dashboard says: "no machine-readable review verdicts exist for this period" rather than
> implying all-clear. *(`coverage.gaps`, data-driven refusal §3.3.)*
>
> **Cost: not capturable for this run.** No token/model/cost fields exist in any forge or
> guardkit durable record for FEAT-3ED2 (verified: zero hits in forge; guardkit LLMCallEvent
> existed but the NullEmitter discarded it). `cost_summary(FEAT-3ED2)` returns
> `coverage.captured_share=0` with the uninstrumented note — ask A-4 is the fix.
> *(`usage_frontier` empty; honesty per DDR-DASH-004.)*

**Gate verdict on the example:** the three questions ARE answerable for FEAT-3ED2 — progress and
on-track fully from live-now feeds (with the bootstrap path doing the work this pre-dashboard
run requires), issues answered honestly with two named gaps that map one-to-one onto wire asks
(A-4, A-5, A-7). The design is *done* by the kickoff's bar: every line names its projection and
its payload, existing or asked.

---

*Corrections require a dated note, never a silent edit. Field-level asks extracted from every 📋
row live in `wire-consumer-requirements-2026-07-08.md`; sequencing in `build-plan-2026-07-08.md`.*
