# Capability Verification — what the dashboard wires onto, verified against the live factory

**Status:** COMPLETE · 2026-07-11 · pre-Phase-B verification requested by Rich ("verify the
actual capabilities it wires onto exist in the infrastructure of the factory"). Four read-only
repo verifiers (runs `wf_a1682046-692` + a nats-core re-run) + live-surface probes on the GB10,
executed by the Fable coordinator. **Repos pinned at:** nats-core `e46a419` · nats-infrastructure
`f008c05` · forge `c583050` · jarvis `73b8476` (all clean trees). Live probes ran while
Session A held the jarvis/forge/nats-infrastructure lanes (live credential rotation) — repo
reads were read-only; transients are flagged where relevant.
**Consumers:** the Phase-B build (ux-spec §9 stages bind to this note's dispositions) · the
design/wire docs (dated addenda added same day, pointing here) · Rich.

---

## 1 · What HOLDS (verified, build on it)

| Capability | Evidence |
|---|---|
| Stream topology exactly as designed — PIPELINE `work`/7d/10k (the IN-1 fence premise), AGENTS limits/24h/5000, FLEET 1h/5000, JARVIS 1h/1000, MEMORY 365d/100k | stream-definitions.json; zero commits touched streams/accounts since the 2026-07-08 design snapshot |
| FINPROXY = exactly one user `mark` scoped `finproxy.>`; **zero cross-account exports/imports anywhere** — transport tenancy firewall intact | accounts.conf.template:163-175 |
| IN-3 confirmed unlanded: no `dashboard_ro`, no `dash-*` consumers, provision-streams.sh creates no consumers | repo-wide greps |
| Build lifecycle payload family complete (all 8) + **BuildCompletePayload carries all seven ledger fields** incl. `pr_url` + task counters (cross-total validated) | _pipeline.py:259-286; envelope registry :141-150 |
| StageCompletePayload full (stage_label, status, gate_mode, coach_score, duration_secs); forge `stage_log` matches (gate_mode/coach_score/threshold_applied) — **126 live rows in prod** | _pipeline.py:934-965; forge schema.sql:66-91; prod DB probe |
| Approval loop BOTH halves wired: forge publishes ApprovalRequestPayload (incl. crash-recovery re-issue); **jarvis publishes ApprovalResponsePayload (decision, decided_by = verbatim Slack clicker)** on `agents.approval.forge.*.response` | forge approval_publisher.py:436, recovery.py:312-338; jarvis slack_reply.py:394-411,606-616 |
| **P5's live-arrival emitter has LANDED** (design's "🟡 inert until MP-010/J04" is stale): jarvis slack_planning_intake publishes PlanningQueuedPayload → `pipeline.planning-queued.{cid}`; config-gated (four intake keys on the running jarvis) | slack_planning_intake.py:204-213,340-383 |
| forge emitters wired for all 8 build-* events + stage-complete (static verification; live emission = S2's job); forge fleet.register on boot is fatal-on-failure | pipeline_publisher.py:109-118; cli/serve.py:852 |
| planning_runs/planning_run_events schema_v3 (states QUEUED…PLANNED_HANDOFF, defer/escalation) present in the PROD DB | forge schema_v3.sql:28-92; prod DB probe (0 rows — front half not yet run through prod, as the design says) |
| MemoryEpisodeV1 + `memory.episode.>` + MessageEnvelope decode contract (event_type registry; **envelope correlation_id is nullable — guard it**) | _memory.py:16; topics.py:184-185; envelope.py:183-235 |
| Live broker healthy: FLEET carrying 524 msgs (heartbeats flow TODAY → P1 has real substance), MEMORY 829, PIPELINE 6; NATS :8222 ok; llama-swap :9000 ok + model list | :8222/jsz probe 2026-07-11 |

## 2 · DRIFT the build must absorb (dispositions — folded into ux-spec v1.3 + dated addenda)

1. **TWO FORGES — the design's default DB path points at the WRONG one.** The live factory is
   the `forge-prod` container; its state is bind-mounted at
   `/home/richardwoollcott/forge-prod-state/.forge/forge.db` (full schema incl. planning
   tables + lifecycle-bridge registry; Session A backups beside it). The `FORGE_DB_PATH`
   default `~/.forge/forge.db` is a **stale dev DB** (builds empty, no planning tables, June
   24 data) still touched by a running `langgraph dev` process. **Disposition:** the
   dashboard's forge-mirror path is explicit config, pinned on this host to the prod
   bind-mount; the two-forges trap is documented in the S2 stage text.
2. **`pr_url` has NEVER been populated** — 0 rows in the prod DB *and* the 2026-07-06 backup;
   the only COMPLETE build (FEAT-9E59) has none. June's real deliveries (FEAT-3ED2/DD4F) rode
   guardkit outside the forge daemon and exist only in guardkit artifacts, which the dashboard
   by design never scrapes. **Disposition:** the Delivered page at launch is honestly
   near-empty; it fills when forge-daemon builds complete with PRs (WS2-V1 window w/c 07-14
   is the feed-in event). M-D2's parity sweep will name these as feed-gap discrepancies —
   the measure working as designed, recorded not hidden. FEAT-3ED2 remains a TEST fixture.
3. **P1 roster is dual-source.** jarvis registers via the `agent-registry` KV bucket ONLY
   (periodic KV re-put = its heartbeat; no fleet.* subject publish); nats-core's
   `register_agent` does BOTH subject + KV; some agents publish FLEET subjects (524 live
   msgs). **Disposition:** the roster projection consumes BOTH: fleet.register/heartbeat/
   deregister subjects (live) AND a **plain core-NATS subscribe on `$KV.agent-registry.>`**
   (KV puts ride that subject space — no JetStream API, no consumer: the zero-JS fence holds).
   Initial KV state is honestly-dark until each agent's next re-put (bounded by its heartbeat
   period). IN-3's subscribe list needs `$KV.agent-registry.>` added — dated amendment filed
   in the wire note.
4. **forge's heartbeat_loop is defined but never started** (fleet_publisher.py:230-305 exists,
   wired nowhere) — forge registers once at boot, then never heartbeats. **Disposition:** the
   roster renders register-only agents as "registered ⟨ts⟩ — no heartbeat feed" (a named gap,
   NOT a fake stale-alarm). Ask A-10 filed: start the existing loop (a forge-lane one-liner;
   owners decide).
5. **`pipeline.stage-gated` has NO producer anywhere** — and the StageGatedPayload contract
   diverges from StageComplete (`stage` not `stage_label`; 2-value lowercase gate_mode; no
   status/duration_secs). **Disposition:** P3 derives GATED state from
   StageComplete.gate_mode + SQLite stage_log; never waits on a stage-gated subject; if one
   ever fires, parse the divergent field names.
6. **BuildProgressPayload has NO task counters** — fields are wave, wave_total,
   overall_progress_pct, elapsed_seconds only; task counters arrive ONLY at BuildComplete.
   **Disposition:** in-flight rows render wave-level progress + pct (ux §4.1 corrected);
   task counts appear at completion. Additionally forge's SQLite `builds` has no task
   counters either (bus-only fields) — `source='forge_sqlite'` rows render counters as
   unmeasured (design §1 F-6 already pinned this).
7. **`slot_id` does not exist on ApprovalRequestPayload** — the key is `request_id`
   (decision enum is 4-valued: approve|reject|defer|override). **Disposition:** approvals
   keyed on request_id end-to-end; the design §4.5 `slot_id` column is vestigial (nullable,
   unfed).
8. **`extra="ignore"` on the v1 payloads** (BuildProgress, BuildComplete, all _fleet/_agent/
   _memory, MessageEnvelope) vs `extra="allow"` only on v2.2 payloads. Unknown fields are
   silently DROPPED at parse. **Disposition for the dashboard:** none (we consume known
   fields). **Disposition for the wire:** the A-4 usage-rollup ask's "payloads are
   extra=allow, the block is additive" premise is only true for v2.2 models — an A-4 rollup
   on BuildComplete requires a nats-core model bump or it is silently stripped. Dated
   correction filed in the wire note.
9. **No feature title exists anywhere in the factory today** — grep-proven across every
   nats-core payload (nearest: BuildComplete.summary, PlanningQueued.request_text); no title
   column in forge state; jarvis's planning intake carries none ("feature identity is the
   planning chain's OUTPUT"). **Disposition:** ux §5.8's id-fallback + named gap stands as
   the v1 reality; ask A-9 filed (Mode-P-minted title carried on BuildQueued/BuildComplete).
10. **No read-only APPMILLA credential exists** — every scoped user also publishes;
    only `rich`/`james` subscribe the full space; forge/jarvis carry `$JS.>` and are
    workqueue actors. **Disposition:** S2's dev-creds interim borrows `james` (least
    surprising human cred; projector env only), with read-only enforced by the build's own
    discipline (zero publish call sites, zero JS API — M-D3 grep) rather than credential
    scope. IN-3 remains the real fix; urgency note added to the wire amendment.
11. **`jarvis.>` carries no jarvis output** — it is the inbound forge→jarvis notification
    channel; jarvis's real output rides `pipeline.>`/`agents.>`/`memory.>` (all already in
    the IN-3 subscribe list — no break, recorded for accuracy).
12. **LiteLLM :4000 was DOWN at probe time** (connection refused; llama-swap + NATS healthy;
    possibly a Session A transient). No v1 impact (panels make zero model calls; P6 renders
    it red honestly); D4's serving path depends on it — recheck at D4.
13. Minor: design's topics.py cite for `for_project` is stale (now :232-260); envelope
    `correlation_id` nullable — projector guards.

## 3 · Asks filed by this note (asks only — owners decide)

- **A-9 (to WS2 B7 / nats-core):** carry the Mode-P-minted feature `title` on
  BuildQueuedPayload + BuildCompletePayload (and the spec-ready handoff event). Until then
  the dashboard renders id-fallbacks (ux §5.8).
- **A-10 (to the forge lane):** start the existing-but-unwired `heartbeat_loop`
  (fleet_publisher.py:230-305) in the daemon boot path, so forge's roster row goes live.
- **IN-3 amendment (to WS5):** add `$KV.agent-registry.>` to `dashboard_ro`'s subscribe list
  (core-NATS subject-space read of KV puts; no JS API grant needed). Plus the no-read-only-
  cred finding raises IN-3's priority: until it lands the projector runs on a borrowed human
  credential.

---
*Verification run 2026-07-11 by the factory-dashboard Phase-A coordinator session; evidence
transcripts under the session's workflow dir (`wf_a1682046-692`). Corrections to this note by
dated note.*
