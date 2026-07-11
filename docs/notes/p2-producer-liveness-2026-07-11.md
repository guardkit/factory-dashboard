# P2 producer liveness — read-only live observation (S2 / D1)

**Status:** 2026-07-11 · the ONE permitted live-broker touch of the S2 stage (ux §9.2 / fence 3):
a **read-only core-NATS subscribe** against the live broker (`127.0.0.1:4222`, the
`ships-computer-nats` container). NO JetStream API, NO consumer bound on PIPELINE, NO ack, NO
publish — a plain `nc.subscribe` observer, drained after the window. Two ~25 s windows were
observed. Evidence script: session scratchpad `p2_liveness_probe.py` (subscribe-only).

## Credential caveat (honest, per fence 3)

The intended `dashboard_ro` (IN-3) does not exist yet, and no `james` credential was discoverable
in the environment mid-rotation (Session A holds the live credential lanes today). The only
discoverable subscribe-capable credential was **`forge` (`FORGE_NATS_URL`)**. The observation used
it read-only (subscribe only — a core subscribe does not consume/ack JetStream work, so it does not
compete with forge's workqueue consumer; fences 1-2 hold). This is an interim measurement cred, not
the projector's runtime cred; the projector runtime still borrows `james` per drift 10 once
available, and IN-3 remains the real fix.

## What fired live (observed)

| Subject (family) | event_type | Count / 25 s | Field presence (payload keys) |
|---|---|---|---|
| `fleet.heartbeat.{agent_id}` | `agent_heartbeat` | 3–3 per window | `agent_id, status, queue_depth, active_tasks, uptime_seconds, last_task_completed_at, active_workflow_states, metadata` — **matches AgentHeartbeatPayload in full** (design §2 P1 ✅) |
| `pipeline.planning-queued.{cid}` | `planning_queued` | 1 (2nd window) | `correlation_id, request_text, target_repo, originating_user, originating_adapter, parent_request_id, queued_at, requested_at, retry_count, stage, triggered_by` — **confirms P5's live-arrival emitter has LANDED** (capability note item; design's "🟡 inert" is stale) |
| `$KV.agent-registry.{agent}` | (KV re-put; e.g. `…jarvis`) | 1 (2nd window) | KV re-put observed on the subject space as a plain core message — **confirms the dual-source roster read works without any JS/KV API** (drift 3). The value did not decode to a keyed JSON dict in the window (empty/opaque body); the projector treats the subject key as the agent id and the re-put receipt as the KV liveness, which is exactly what P1's `project_kv` does. |

## What did NOT fire in the window (recorded, not hidden)

- **No build-lifecycle events** (`build-queued/started/progress/paused/complete/failed`) fired in
  either window. Consistent with the capability verification: forge-daemon builds that emit the
  live P2 family land with WS2-V1 (w/c 07-14); there was no active build during observation. P2's
  live path is therefore **contract-verified (the payloads match nats-core) but not
  producer-verified live** in this window — the design §2 P2 `✅/🟡` split stands. The projector's
  P2 projection is exercised end-to-end in tests against the ephemeral broker with these exact
  payloads.
- **No `pipeline.stage-complete` / `stage-gated`** fired live — consistent with drift 5 (no
  stage-gated producer anywhere; P3's GATED derives from `StageComplete.gate_mode` + the forge
  `stage_log` backfill, which the mirror handles).
- **No approval events** (`agents.approval.*`) fired in the window (no approval was pending).

## Permission finding (new, recorded for the wire)

The `forge` credential got a **permissions violation subscribing to `memory.>`** ("nats:
permissions violation for subscription to memory.>"). It DID subscribe cleanly to `pipeline.>`,
`agents.>`, `fleet.>`, and `$KV.agent-registry.>`. This reinforces IN-3's urgency and the drift-10
finding: `forge` is a scoped workqueue actor, not a full-space reader — the projector's real cred
must be `dashboard_ro` with the four-subject read grant (incl. `$KV.agent-registry.>`, per the wire
note's IN-3 amendment). MEMORY-stream reads (P11 episodes, a later phase) are out of scope for v1
and were not required here.

## Feature-title reality (§5.8 — the load-bearing check)

**No observed live payload carried a human feature title** (`title` / `feature_title` /
`feature_name`) — `title_carrying_payloads` was empty across both windows, incl. the
`agent_heartbeat` and `planning_queued` payloads. This **confirms ux §5.8's feed reality and
capability-note drift 9**: no title feed exists on the operator side today. The nearest carriers
remain `BuildComplete.summary` and `PlanningQueued.request_text` (neither a minted title). The
dashboard's id-fallback + named gap ("N features have no projected title — title feed pending")
stands as the v1 rendering.

**Ask re-confirmed toward B7 (asks-only, owners decide):** **A-9** — carry a Mode-P-minted feature
`title` on `BuildQueuedPayload` + `BuildCompletePayload` (and the spec-ready handoff event). Until
it lands, the id-fallback is the honest rendering. (No new ask is filed by this note beyond
re-confirming A-9 and re-noting IN-3's `memory.>`/full-space grant urgency.)

## Limitations of this measurement

- Two short (~25 s) windows during a quiet period — absence of build/stage/approval traffic is
  "not observed in-window," not "producer absent." The contracts are verified against nats-core on
  disk; live producer verification for the P2 build family is a WS2-V1-window (w/c 07-14) re-check.
- Measured on the `forge` credential, not the projector's intended `dashboard_ro`/`james`; the
  `memory.>` denial is a property of that cred's scope, recorded for IN-3.

*Recorded 2026-07-11 by the S2 (D1) build session. Corrections by dated note. The design doc is
unedited; this note carries the evidence (ux §9.2).*
