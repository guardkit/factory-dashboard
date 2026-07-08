# Factory Dashboard — Build Plan

**Status:** v1 · 2026-07-08 · from the fleet-dashboard architecture-refresh session (kickoff
P19). Sequences the arch/design pair into build sessions. **Nothing below needs Fable** — the
judgment-dense work (contracts, tenancy, status model, wire asks) is discharged into the three
companion docs; builds are [Opus 4.8]/autobuild against pinned designs.
**Standing rules (house):** one repo per session (venue = `factory-dashboard` throughout; the
few external touches are asks already filed, not sessions here); commit+push per artifact;
dated notes; supersession banners; every session carries a Model: tag; estimates in house S/M.
**GB10 calendar (program plan §2.2, binding):** the ~90h Phase-3 run owns the box 07-09 eve →
~07-13 late; 07-09 is the HSBC demo quiet day. Dashboard **coding** sessions are CPU-light and
venue-isolated — fine anytime; **autobuild-lane** runs use the local Coach (GPU) — schedule
after ~07-13 or run degraded; anything touching the live broker/gateway waits for WS5/B9
windows regardless.

---

## 0 · Phase index and dependency edges (drawn honestly)

```
D0 scaffold ──► D1 read-model core + live-now panels ──► D2 interim ledger (Rich)
                                    │                        │
WS5 IN-3 (dashboard_ro user) ───────┘ (D1 can start on rich/dev creds; IN-3 before any
                                        long-running deploy of the projector)
WS5/B9 IN-4 + B8 A-8 (per-tenant feed pair; first: FinProxy) ──► D3 client ledger view(s) go live
D1 ──► D4 chat v1.x (tools + grounding + LiteLLM serving)
IN-5 (gateway Postgres + keys) ──► D5a gateway cost slice
B7 A-4 (+A-4b guardkit emitter) ──► D5b frontier cost slice ──► D5c cost panel complete
B7 + B8 (deploy/live-gate payloads + stage) ──► D6 back-half panels + ledger graduation
WS1-E build / WS4-S7 / WS4-S4 (episode producers) ──► D7 flywheel panels (episodes)
D4 ──► (later, out of plan) PO-panel seam · jarvis Slack twin
```

No dashboard phase blocks any factory workstream; every inbound gate is an ask already filed in
`wire-consumer-requirements-2026-07-08.md`.

## 1 · Sessions

| # | Session | Scope (design-doc anchor) | Model | Est. | Earliest |
|---|---|---|---|---|---|
| D0 | **Scaffold + skeleton** | file tree (design §8), FastAPI app, schema.sql (§4), config; no NATS yet | [Opus 4.8] attended | S | now |
| D1 | **Read-model core + live-now panels** | projector conn A (dev creds until IN-3 — **PIPELINE via core-NATS subscribe ONLY; no JetStream consumer may be created, bound, or acked on PIPELINE under any credentials until IN-1 resolves**, design §2 P2), projections P1–P6 (P6 pinned to load-neutral endpoints — LiteLLM `/health` forbidden), forge SQLite mirror (WAL-courtesy discipline, design §4.7), watermarks, SSE (§6), HTMX panels; **records the P2 producer-liveness verification** (which build-lifecycle events actually fire — the matrix's ✅/🟡 sharpened with evidence) | [Opus 4.8]; autobuild-able (GPU: post-07-13, or degraded) | M | now (code) |
| D2 | **Interim delivery ledger (Rich view)** | ledger table + bootstrap from forge `builds` + build-complete consumer; period query; bar labelled `merged_pr` (§5) | [Opus 4.8]; autobuild-able | S–M | after D1 |
| D3 | **Client-tenant view** (tenant-parameterized; first instance: FinProxy) | tenant registry config (`tenants.yaml`, design §4.0); per-tenant projector connection → `ledger_client_{tenant}.db`; client session + panel + DF-008 field firewall tests; per-project rollups across the tenant's repo set. Acceptance includes proving a **second** tenant is config + provisioning only (a dry-run registry row with no account must render "feed pending", zero code change) | [Opus 4.8] attended (tenancy = security-relevant) | M | after the first tenant's IN-4 + A-8 land |
| D4 | **Delivery-status chat v1.x** | tool registry + 5 tools (§3), grounding checker, LiteLLM client (`chat`/`workhorse`), chat window + SSE streaming; FinProxy registry binding | [Opus 4.8] attended (grounding checker is correctness-critical) | M | after D1 |
| D5a/b/c | **Cost lens** | a: `usage_gateway` from LiteLLM spend table; b: `usage_frontier` from A-4 payloads; c: `cost_rollups` + panel + `cost_summary` tool with coverage notes | [Opus 4.8] | S each | a: after IN-5 · b: after B7+A-4b |
| D6 | **Back-half panels + ledger graduation** | P8/P9 projections, `deployed_live_verified` bar appends, evidence links (Rich) | [Opus 4.8]; autobuild-able | M | after B7+B8 (WS2's V1 window ~w/c 07-14 is the natural test bed) |
| D7 | **Flywheel/episode panels** | `memory.episode.>` consumer + per-type panels as producers land | [Opus 4.8] | S | per producer landings |
| — | **PO-panel seam** (later, explicitly out of this plan) | approve action via existing gate mechanism; new narrowly-granted publish permission (ADR-DASH-007) | — | — | not before the factory's own phone loop + B13 ownership decisions mature |

**Suggested calendar shape:** D0+D1 code can run 07-09..07-12 (venue isolated; no GPU needed if
run attended rather than autobuild); D2 immediately after; D4 next (it is the product thesis —
prioritize over D3/D5 if asks are still pending); D3/D5a whenever their operator/infra asks
land; D6 aligns naturally with WS2's V1/V2 validation window.

## 2 · Pre-registered value measures (recorded before any build; judged at each phase's close)

| M | Measure | Bar |
|---|---|---|
| M-D1 | **The three-question test:** for any feature with build events, `feature_status` answers progress/on-track/issues with every line cited (the FEAT-3ED2 worked example is the fixture — design §10) | answerable, cited, refusals data-driven |
| M-D2 | **Ledger parity:** `delivered_period` vs a manual git/PR sweep over the same window | 100% agreement or each discrepancy explained by a named feed gap |
| M-D3 | **Projection-not-participant audit:** broker-side — `dashboard_ro` has zero publish grants and no `$JS.>` grant (IN-3 as re-cut); app-side — no NATS publish call sites; **plus (gate extension): no JetStream consumer exists on any workqueue stream; all dashboard consumers are the WS5-pre-created `dash-*` set on limits streams; ack policy is explicit; acks are never sent on PIPELINE subjects** (library-level JS API/ack publishes are exactly what a grep for `publish(` misses) | all hold; checked at D1, D3, D4 close |
| M-D4 | **Tenant-leak probe:** from each configured client-tenant session, attempt every panel/tool/SSE channel + crafted queries for ids outside that tenant's project set (incl. other tenants' ids once ≥2 exist) | zero rows cross; probe scripted per tenant, kept as a regression test |
| M-D5 | **Chat grounding:** N=20 mixed questions (answerable + unanswerable) | 0 fabricated records; every unanswerable → named gap; grounding-checker rejections logged |
| M-D6 | **Cost coverage honesty:** cost panel always states captured-share | no view presents partial capture as total (DDR-DASH-004) |

## 3 · Risks

| Risk | Mitigation |
|---|---|
| Build-lifecycle producer liveness overestimated (P2 🟡) | D1 verifies and records; bootstrap path (forge SQLite) carries the ledger regardless |
| Client-tenant feed asks (IN-4/A-8) stall | that tenant's view stays honestly dark; D3 is the only gated audience-facing phase; Rich's value lands in D1–D2 regardless |
| Chat over-trusted before M-D5 passes | chat ships behind Rich-only flag until M-D5 recorded; FinProxy chat only after M-D4+M-D5 both green |
| GPU contention with the 90h run | no dashboard session needs the GPU except autobuild lanes and chat serving tests — both post-07-13; calendar rule in header |
| Runtime chat turns evicting the Coach's model mid-build (llama-swap swap-in) | arch §7.5 no-eviction rule: pin to resident model or degrade to raw tables during active builds; D4 implements the resident-model check |
| Scope creep toward participant | ADR-DASH-007 + M-D3 audit at every phase close |

---

## 4 · GATE RECORD — in-session adversarial review (2026-07-08)

Per the kickoff gate: three reviewers, each briefed to attack one failure class (tenant-scope
leak · chat fabrication · projection→participant drift), run against the committed arch/design/
wire docs; findings fixed in-doc or filed here with disposition. The worked example
(FEAT-3ED2) is design §10 and passed its own bar. **Results appended below after the review
pass — this section is written before the reviewers run (pre-registration).**

> **Dated note 2026-07-08 — GATE RUN, ALL FINDINGS DISPOSED. 29 findings (5 BLOCKER / 14
> MAJOR / 8 MINOR / 2 NOTE-sound-confirmations beyond the ones listed): 28 FIXED in-doc, 1
> ACCEPTED-AS-DESIGNED.** The worked example was itself corrected by the review (it had
> exercised two of the holes it existed to test — the honesty machinery worked one level up).
>
> **Reviewer T (tenant-scope leaks) — 2 BLOCKER, 4 MAJOR, 2 MINOR; all FIXED:**
> T1 BLOCKER `finproxy.>` is directly client-readable, so A-8-as-"copies" would have leaked
> internal payloads (tasks_failed, branch, evidence refs) past DF-008 → A-8 re-cut to a
> distinct reduced client-facing event (wire §4; arch §4). T2 BLOCKER FinProxy cost had no
> structurally compliant feed (all cost data is APPMILLA-side; app-copying would violate
> ADR-DASH-002) → per-build spend rides the A-8 reduced event; per-project = in-store sum
> (arch §4/§8; design §4). T3 "audience-filtered view" language reintroduced WHERE-clause
> tenancy → FinProxy cost handler reads `ledger_finproxy` only (design §2). T4 FinProxy tool
> registry underspecified / 3 of 5 tools unsatisfiable from the client store → registry
> enumerated to three `_lite` tools; `ledger_client` reduced DDL drops `evidence_ref` (design
> §2/§4). T5 host-level Tailscale share exposed :8222/:9000/:4000 → port-scoped ACL (arch §5).
> T6 operator parity path co-opened both DBs → offline batch job, own credential (design §7).
> T7 degrade-to-table could dump unclean fields → FinProxy tool results DF-008-clean at the
> tool boundary (design §2). T8 tenant-from-cookie fixation → tenant re-read from `users.tenant`
> per request (design §7). Sound-confirmed: SSE notification-only design; ADR-DASH-007;
> the honestly-dark fallback.
>
> **Reviewer F (chat fabrication) — 2 BLOCKER, 6 MAJOR, 3 MINOR; all FIXED:**
> F1 checker verified citation existence, not entailment → scalar claim-matching (design §3
> rule 3). F2 uncited sentences vacuously passed → per-sentence citation tokens (rule 2).
> F3 model arithmetic (incl. summing the never-sum currencies) → no numeral not present in
> tool results; tools pre-compute (rule 4). F4 ETAs/predictions had no gap to trigger refusal
> → deterministic forecast-class filter (rule 5). F5 watermark lag rendered as "now" +
> projector-stall manufacturing false stalled-issues → `as_of` on every response, lag
> displayed, stalled-detection gated on projector liveness (§3 envelope; §1). F6 unmeasured
> norms counted as zero-green (turns/ceiling have no feed) → measured-or-excluded rule (§1) +
> the A-4 loop-stats sub-ask (wire §2). F7 the worked example overclaimed four figures →
> §10 rewritten (unmeasured signals as gaps; re-work amber demoted to a coverage gap). F8 gaps
> could be silently dropped → string-containment enforcement (rule 6). F9 degrade-to-table
> lost the question → labelled header (rule 7). F10 window/scope uncitable + period double-
> counting → window echo (§3) + upgrades excluded from `delivered_count` (§5). F11 multi-turn
> re-assertion → prior assistant prose excluded; every turn re-queries (rule 1).
>
> **Reviewer P (projection→participant drift) — 1 BLOCKER, 4 MAJOR, 5 MINOR; 9 FIXED, 1
> ACCEPTED:** P1 BLOCKER a JS consumer on workqueue-PIPELINE could consume-delete forge's work
> items; the live path was unpinned → core-NATS-subscribe-only rule (design §2 P2; D1; IN-1
> clarified). P2 IN-3's `$JS.>`+zero-publish was self-contradictory; realistic repair was a
> broker-write grant → IN-3 re-cut: WS5-pre-created `dash-*` consumers, no `$JS.>` grant, scoped
> escape hatch recorded (wire §4). P3 M-D3 blind to library-level JS-API/ack publishes →
> extended (this doc §2). P4 LiteLLM `/health` fires real completions → load-neutral endpoints
> pinned, `/health` forbidden (design §2 P6). P5 chat turns evicting the Coach's model →
> no-eviction rule (arch §7.5; risk row above). P6 WAL reader starving forge's checkpoints →
> courtesy discipline + failure trigger (design §4.7). P7 A-8 emission could couple the
> pipeline to the FinProxy feed → best-effort/non-blocking clause (wire §4). P8 approve-action
> permission would land on `dashboard_ro` → separate `dashboard_approve` user, session-user
> identity (arch ADR-DASH-007). P9 `_INBOX.>` account-wide eavesdropping → `_INBOX.dash.>` +
> `inbox_prefix` (wire IN-3). P10 web-layer rw-default SQLite opens → `mode=ro` + M-D4
> assertion (design §7). P11 NOTE: A-4/A-4b consumer-driven producer work judged acceptably
> fenced → **ACCEPTED as designed** (additive, rollup-only, owners-decide framing kept).
> Sound-confirmed: the refused app-side bridge, SSE push, chat-originates-nothing, IN-1-guarded
> replay, panels-zero-model-calls.
>
> **Gate verdict: PASSED with fixes applied** (commits: this one). Nothing was filed outward —
> every finding was fixable in the doc set. The three failure classes now each have a
> mechanized guard: producer-side DF-008 reduction at the account boundary (tenant), an
> entailment/completeness/freshness checker (fabrication), and broker-enforced zero-publish
> with workqueue-consumer prohibition (drift).
