# Wire Consumer Requirements — the dashboard as a NAMED CONSUMER of WS2 B7 / WS1-I (+ infra asks)

**Status:** v1 · 2026-07-08 · from the fleet-dashboard architecture-refresh session (kickoff
P19). **Asks only — B7 owns the schemas, WS1-I owns its items, B9/WS5 own the broker.** Nothing
here re-designs another session's deliverable; where a field is already obligated by the
backward-edge episode contract (`fleet-memory/docs/design/backward-edge-episode-schema-contract-2026-07-07.md`
§7), this note **co-signs and cites** rather than re-specifying.
**Consumers:** the WS2 B7 session (nats-core), WS1 Session I (nats-core), WS2 B8 (forge), WS2 B9
(nats-infrastructure), the dgx-spark gateway owner, and the WS3 guardkit lane (one
instrumentation item). Cross-referenced from the WS2 build plan §B7 block (dated pointer) and
`factory-program-plan §7`.
**Why each ask exists:** every ask maps to a 📋 row in the design doc's §2 matrix or to a line
of the FEAT-3ED2 worked example (`factory-dashboard-system-design-2026-07-08.md` §10).

---

## 1 · What we deliberately do NOT ask for (it already exists — verified 2026-07-08)

- `pr_url`, `tasks_completed/failed/total`, `duration_seconds`, `repo`, `branch` on
  `BuildCompletePayload` (nats-core `_pipeline.py:200-238`) — the v1 ledger's entire substance.
- `coach_score`, `gate_mode`, `duration_secs`, `stage_label` on `StageComplete`/`StageGated`;
  gate records in forge SQLite `stage_log`.
- `decided_by` (observed clicker) on `ApprovalResponsePayload`; heartbeat fields on
  `AgentHeartbeatPayload`.
- Turn counts, per-task durations, test counts, arch/honesty scores — guardkit TurnStateEntity +
  TaskCompletedEvent already carry them; the dashboard reads outcomes from bus/SQLite, not from
  these files, but does not ask the wire to duplicate them.
- Per-call token fields — guardkit `LLMCallEvent`
  (`orchestrator/instrumentation/schemas.py:146`) already defines provider/model/input_tokens/
  output_tokens/latency. See A-4: the ask is **rollups + a persistent emitter**, not new
  per-call fields, and explicitly **not** per-request events on the bus (volume).

## 2 · Asks to WS2 B7 (nats-core payload session)

| # | Ask | Detail | Overlap note |
|---|---|---|---|
| A-1 | **Timestamps + correlation on every deploy payload** | `DeployQueued/Started/Complete/Failed` each carry their own event timestamp and `correlation_id`; Complete/Failed carry `env_id`, `artifact_digest`/`image_digests`, `deploy_record_ref`, `failed_step` (on Failed), `duration_seconds` | fields beyond timestamps are already B7's own list + backward-edge §7.8 — co-signed, dashboard consumes them for panel P8 |
| A-2 | **`attempt` semantics** | live-gate results carry `attempt` (monotonic per correlation_id across campaigns) so "on track" can count re-runs; instrument/environment re-runs distinguishable via the verdict enum so they are **never** counted against the feature | already obligated on B4/B7 via backward-edge §7.7/§7.9 — co-signed verbatim |
| A-3 | **Verdict payloads mirror the envelope, with evidence refs** | `QAVerdictPayload`/`LiveGateResultPayload`: verdict enum four-for-four (`pass|fail|instrument_fail|environment_fail`), `gate_ids`, per-assertion `{id, gate_id, status, disposition, evidence_ref}`, `evidence_index_ref` (F5), `app_url`, `run_id` | already B7's mirror-the-envelope guardrail — co-signed; the dashboard renders `evidence_index_ref` as links (Rich view only, DF-008) and graduates ledger rows on counting `pass` (design §5) |
| A-4 | **THE COST ASK (new): per-stage usage rollup block** | an optional repeated block on `StageComplete`-class and `BuildComplete`-class payloads (and deploy/live-gate payloads if models run there): `usage: [{lane: frontier\|local, provider, model, calls, input_tokens, output_tokens, cost_gbp?}]` — **aggregated at the producer** from LLMCallEvent-class instrumentation; `cost_gbp` nullable (local lanes have no real £; dashboard applies nominal pricing, labelled nominal). This is the frontier-lane capture point for spend that never transits the LiteLLM gateway (the Claude SDK Player). Rollups only — never per-request events on the bus. **Loop-stats sub-ask (gate addition 2026-07-08):** the same rollup block (or a sibling `loop_stats` block) SHOULD carry `turns` and `sdk_ceiling_hits` counters per stage/build — two of the on-track norms have no live feed without them (design §1 measured-or-excluded rule) | genuinely new; nothing in nats-core 0.5.0 carries any usage field (verified). Companion item A-4b below is the guardkit half |
| A-4b | **(guardkit lane, WS3 or B-series):** wire a persistent `LLMCallEvent` emitter (default today is `NullEmitter` — events discarded, `agent_invoker.py:40`) and fix the `model="default"` fallback (`agent_invoker.py:4422`) so A-4's rollups have true model names | without this, A-4's block would be zeros — the ask is stated here so B7 doesn't ship a field no producer can fill | new; flagged to the WS3/guardkit lane, not owned here |
| A-6 | **Tenant-prefix convention documented as normative** | the `Topics.for_project(project, topic)` prefix (`topics.py:183-198`) + envelope `project` field documented in the topics registry as THE tenant fan-out convention for client-visible delivery events. **Amended 2026-07-08 (Rich): the leading token is the TENANT slug (account-aligned), not a repo name** — a client tenant may own several repos, so the reduced event's payload carries the `project`/repo while the subject prefix carries the tenant (first instance: `finproxy.delivery….{feat}`). Pin the shape so it is a convention, not an ad-hoc string | convention exists in code; the ask is normative documentation (same genre as WS1-I item 2's `plan-{cid}` ask) |
| A-7 | **Machine-readable merge-review verdicts (F14 lineage)** | when the F14 review-record format (B10) and the adversarial merge gate (WS3-S5) land, a bus notification or episode referencing the record (feature_id, verdict, findings count, record ref). The FEAT-DD4F catch — 16/16-confirmed post-merge findings — is invisible to any projection today; this is the worked example's named blind spot | filed toward B10/WS3-S5 as a consumer requirement; B7 need only leave subject-space for it |

## 3 · Asks to WS1 Session I (nats-core planning payloads)

| # | Ask | Detail | Overlap note |
|---|---|---|---|
| A-5 | **Planning lifecycle events carry what the timeline panel needs** | `planning_started/complete/failed` with `correlation_id`, `mode`, `originator` (observed member id), event timestamps; the spec-ready handoff event carries `correlation_id` + Mode-P-minted `feat_id` + output refs | ALL already obligated by backward-edge §7.2/§7.3 — co-signed; the dashboard is a second named consumer, no new fields |
| A-5b | *(optional, decline freely)* terminal planning events carry a count/refs of deferred operator items (the TASK-MP-010 case) | if declined, the dashboard reads deferred items from its forge-SQLite mirror — acceptable; the ask exists only because bus-fed is cleaner than mirror-fed | new but optional |

## 4 · Infra + producer asks (not nats-core)

| # | Owner | Ask |
|---|---|---|
| IN-1 | **WS2 B9** (nats-infrastructure) | **Observer-readable pipeline history:** PIPELINE is `retention: work`, 7d/10k (`streams/stream-definitions.json:4-13`) — workqueue semantics make it single-consumer consume-on-ack; an observer cannot replay or even co-consume safely. Requirement (mechanism is B9's choice): limits-retention change, a mirrored observer stream, or a documented ruling that observers get **core-NATS subscribe only** on PIPELINE subjects (the dashboard's read DB tolerates that — bootstrap comes from forge SQLite either way, design §5). **Clarified 2026-07-08 (gate):** "live-subscription-only" means a plain core-NATS subscription — ANY JetStream consumer on a workqueue stream is destructive/competing; the dashboard binds none, ever, until this ruling lands (design §2 P2 rule) |
| IN-2 | WS2 B9 | No change asked on AGENTS (24h)/FLEET (1h)/JARVIS (1h) — noted so the ruling is explicit: the dashboard claims **no** history from these streams |
| IN-3 | **WS5** (accounts, rides a rotation restart) | **(Re-cut 2026-07-08 — gate finding: a blanket `$JS.>` + "zero publish" grant is self-contradictory, since the JS API is request-reply and acks are publishes.)** WS5 **pre-creates** the dashboard's durable push consumers (on limits streams only, names `dash-*`, deliver subjects under `dash.deliver.>`). New APPMILLA user `dashboard_ro`: subscribe on `dash.deliver.>` + `pipeline.> agents.> fleet.> jarvis.> memory.episode.>` (core-NATS live subs) + `_INBOX.dash.>` (client pinned to `inbox_prefix=_INBOX.dash` — no account-wide inbox eavesdropping); **no `$JS.>` grant at all; zero other publish grants**. If self-service consumers are ever needed, publish is scoped to `$JS.API.CONSUMER.{CREATE,INFO,MSG.NEXT}.<named limits streams>.dash-*` with explicit deny on `$JS.API.STREAM.>` and `CONSUMER.DELETE` — recorded here so the lazy widening never happens silently |
| IN-4 | WS5 + WS2 B9 | **The client-tenant feed pair (parameterized — amended 2026-07-08):** per configured client tenant, (a) an APPMILLA→{tenant account} export/import of `{tenant_prefix}.>` (today zero imports/exports exist anywhere), and (b) a `dashboard_{tenant}` subscribe-only user in that tenant's account. First instance: FINPROXY (one existing user, `mark` — `accounts.conf.template:163-175`). Onboarding a second client = repeating this pair + a tenant-registry row, no code change. Until a tenant's IN-4 + A-8 land, that tenant's view is honestly dark (arch §4) |
| A-8 | **WS2 B8** (forge deploy stage — the producer half of A-6) | **(Re-cut 2026-07-08 — gate BLOCKER: a tenant prefix is directly readable by the client's own NATS users, so internal payloads must never appear there. Amended same day, Rich: tenant mapping is CONFIG, never code.)** forge emits a **distinct, DF-008-reduced, client-facing delivery event** on `{tenant_prefix}.>` for any repo that maps to a client tenant, carrying ONLY: feature id, title, **project/repo**, bar cleared (`merged_pr` \| `deployed_live_verified`), delivered date, PR URL, and the per-build spend total (computed at build close from the A-4 rollups forge holds). The **repo→tenant mapping is configuration on the producer side** (one tenant ↔ one or more repos; first instance: FinProxy's repo set) — adding a client or a repo to a client is a config edit, no schema or code change. Never "copies" of BuildComplete/verdict payloads — those carry tasks_failed, branch, evidence refs, all DF-008-forbidden client-side. **Emission is best-effort and non-blocking:** a failed or unauthorized tenant-prefixed publish is logged and dropped — it must never fail, delay, or retry-block the pipeline (the consumer must not reshape the producer's failure modes) |
| IN-5 | **dgx-spark gateway owner** (operator) | Enable the opt-in LiteLLM Postgres layer (`DATABASE_URL` + `master_key`, config bottom block) and mint virtual keys per the **`{project}--{seat}`** convention (arch §8) — per-project spend then falls out of the gateway's own spend table; the dashboard is its first consumer. Until then the dashboard's gateway-cost slice covers only its own chat calls (header capture), labelled as partial |

## 5 · Sequencing note

None of these asks block dashboard Phase 1 (live-now panels + interim ledger — see
`build-plan-2026-07-08.md`). A-1..A-3 shape B7 *while it is still unrun* — that timing is the
point of this note. A-4/A-4b are the cost lens's frontier capture and can land with B7 or as a
follow-up field addition (nats-core payloads are `extra="allow"`; the block is additive).
IN-3/IN-4 ride already-planned WS5 broker-restart windows. IN-5 is an operator session on the
gateway box, independent of everything else.

---

*Asks only; owners decide. Where an owner declines, the dashboard's design already names its
fallback (mirror-fed, coverage-noted, or honestly dark). Corrections by dated note.*
