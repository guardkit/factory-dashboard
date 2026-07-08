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
WS5/B9 IN-4 + B8 A-8 (FinProxy feed pair) ──► D3 FinProxy ledger view goes live
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
| D1 | **Read-model core + live-now panels** | projector conn A (dev creds until IN-3), projections P1–P6, forge SQLite mirror, watermarks, SSE (§6), HTMX panels; **records the P2 producer-liveness verification** (which build-lifecycle events actually fire — the matrix's ✅/🟡 sharpened with evidence) | [Opus 4.8]; autobuild-able (GPU: post-07-13, or degraded) | M | now (code) |
| D2 | **Interim delivery ledger (Rich view)** | ledger table + bootstrap from forge `builds` + build-complete consumer; period query; bar labelled `merged_pr` (§5) | [Opus 4.8]; autobuild-able | S–M | after D1 |
| D3 | **FinProxy view** | conn B projector → `ledger_finproxy.db`; finproxy session + panel + DF-008 field firewall tests | [Opus 4.8] attended (tenancy = security-relevant) | M | after IN-4 + A-8 land |
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
| M-D3 | **Projection-not-participant audit:** broker-side — `dashboard_ro` has zero publish grants; app-side — no NATS publish call sites | both hold; checked at D1, D3, D4 close |
| M-D4 | **Tenant-leak probe:** from a FinProxy session, attempt every panel/tool/SSE channel + crafted queries for non-finproxy ids | zero rows cross; probe scripted, kept as a regression test |
| M-D5 | **Chat grounding:** N=20 mixed questions (answerable + unanswerable) | 0 fabricated records; every unanswerable → named gap; grounding-checker rejections logged |
| M-D6 | **Cost coverage honesty:** cost panel always states captured-share | no view presents partial capture as total (DDR-DASH-004) |

## 3 · Risks

| Risk | Mitigation |
|---|---|
| Build-lifecycle producer liveness overestimated (P2 🟡) | D1 verifies and records; bootstrap path (forge SQLite) carries the ledger regardless |
| FinProxy feed asks (IN-4/A-8) stall | FinProxy view stays honestly dark; D3 is the only gated audience-facing phase; Rich's value lands in D1–D2 regardless |
| Chat over-trusted before M-D5 passes | chat ships behind Rich-only flag until M-D5 recorded; FinProxy chat only after M-D4+M-D5 both green |
| GPU contention with the 90h run | no dashboard session needs the GPU except autobuild lanes and chat serving tests — both post-07-13; calendar rule in header |
| Scope creep toward participant | ADR-DASH-007 + M-D3 audit at every phase close |

---

## 4 · GATE RECORD — in-session adversarial review (2026-07-08)

Per the kickoff gate: three reviewers, each briefed to attack one failure class (tenant-scope
leak · chat fabrication · projection→participant drift), run against the committed arch/design/
wire docs; findings fixed in-doc or filed here with disposition. The worked example
(FEAT-3ED2) is design §10 and passed its own bar. **Results appended below after the review
pass — this section is written before the reviewers run (pre-registration).**

*(Disposition table appended by dated note once the review completes.)*
