# Factory Dashboard — Delivery Read-Model — Conversation Starter

## For: /system-arch + /system-design session · `factory-dashboard` (new repo) · June 2026

---

## Purpose of this document

Context brief for a session that produces **two architecture documents**:

1. **`/system-arch`** — system context (a read-model over NATS), the two-audience
   multi-tenant model, the frontend-stack and FinProxy-access decisions, C4
   diagrams, ADRs, open questions.
2. **`/system-design`** — event→view projections, read-model schema, the ledger
   query, auth/tenant scoping, the push contract, target file tree.

Paste at the start of that session, then generate the two documents
sequentially. Strategic anchor: `factory-scaling-and-output-bottleneck-findings.md`
(D6 — own the spine). The repo `factory-dashboard` exists as a fresh clone.

---

## What is it?

The **delivery dashboard** — a read-model web app over the NATS bus, on owned
hardware. Two audiences, one app:

- **Rich** — operational view: fleet health, work in flight, build and
  deploy/verify status across all projects.
- **FinProxy** — the **delivery ledger**: features delivered this period, with
  PRs, scoped to `finproxy.>`. For the work-as-debt deal this *is* the commercial
  instrument — the legible record of what their deferred fee bought.

It is **not** LAP's session UI. It is a projection of the bus Rich already owns:
no orchestration, no external dependency, cannot be switched off or held to
ransom. Later it hosts the interactive PO panel (James's door, in-app) — but v1
is the read-only delivery + observability view.

---

## The foundation: the bus already emits everything (consume, don't add)

- **Fleet lifecycle** (`Topics.Fleet.*`) — register / deregister / heartbeat
  (status ready|busy, active_tasks, queue_depth, uptime) → the agent roster and
  health panel.
- **Agent results** (`Topics.Agents.RESULT`, envelope-wrapped for event-stream
  consumers — the dashboard is exactly this consumer) — carrying the
  Forge-compatible wrapped output and the weighted-evaluation score.
- **Pipeline events** (dev-pipeline architecture) — `pipeline.feature-planned /
  build-progress / build-complete / build-failed`, `feature_id`-correlated, with
  per-project topic prefixes.
- **Output-side loop events** (from the Forge output-loop doc) — deploy-started/
  progress, verify-result, approval-requested/granted, escalated.
- **Multi-tenancy already exists** — NATS accounts + topic prefixes; the FINPROXY
  account (james, rich_finproxy, mark) scoped to `finproxy.>`.
- **`nats-core`** provides the Pydantic models / `NATSClient` / `Topics` registry
  to consume all of the above in Python.
- **JetStream retains history** — PIPELINE 30d, AGENTS 7d, SYSTEM 24h — so the
  ledger can **replay** "delivered this period," not just show live events.

---

## Key decisions (resolved — do not reopen)

| # | Decision | Resolution |
|---|----------|-----------|
| D1 | Read-model over the bus | The dashboard is a NATS **consumer**; it owns no orchestration state. Source of truth stays NATS (ADR-SP-002). |
| D2 | Owned, zero external dependency | NATS-consumer web app on owned hardware (GB10). Not LAP. Cannot be switched off or held to ransom. |
| D3 | Two audiences, one app, multi-tenant | Rich = full (all projects, fleet, build/deploy). FinProxy = `finproxy.>`-scoped delivery ledger. Reuse the existing NATS multi-tenancy for isolation. |
| D4 | It is the commercial instrument | For work-as-debt, the ledger = "what your deferred fee bought" (features delivered + PRs). This is build #1's *why*. |
| D5 | v1 read-only; PO panel folds in later | Reserve the seam for an interactive PO panel (James's in-app door), but **do not build it in v1** — ship the read-only delivery + observability view. Subtract. |
| D6 | History via JetStream replay | The ledger reads JetStream (30d pipeline retention) for delivered-this-period; live panels use a live subscription. |

---

## Warnings & constraints

- **It is a projection, not a participant.** If the dashboard is down, nothing
  stops. It makes **no writes** to orchestration state — with one deliberate,
  audited exception later: the explicit **approve** action, which publishes a
  gate event (the same mechanism as the output-side loop and the Slack
  approve-to-build). That single write is the only one it will ever make.
- **Tenant isolation is security-relevant.** The FinProxy view must be
  **hard-scoped to `finproxy.>` at the NATS account/subscription level**, not
  merely filtered in the UI. FinProxy must never be able to see another project.
- **No secrets/PII in the ledger or in URLs** (privacy rule). Show delivery
  outcomes, not internal diagnostics — the Forge-compatible result carries a
  Coach evaluation score; surface delivery state to FinProxy, keep agent
  internals to Rich's view.
- **Local first; AWS later is low-risk.** v1 runs on GB10 (Tailscale for Rich);
  the FinProxy access model (Tailscale vs hosted authenticated view) is a
  `/system-arch` decision tied to the hosted-self-serve fork. Cloud migration is
  already low-risk (Docker Compose), but is not a v1 prerequisite.

---

## Open questions for /system-arch to resolve

1. **Frontend stack — the one real unknown.** Backend is clear: FastAPI +
   `nats-core` consumer, WebSocket/SSE push. Frontend options: (a) server-rendered
   + HTMX (minimal JS, fastest to ship, fits a read-model); (b) React/SPA;
   (c) Compose for Web / Kotlin-wasm (existing `composeWebApp` / `kotlinwasm`
   repos). Decide by *who maintains it* and the FinProxy polish bar.
   **Recommendation:** (a) HTMX for v1 speed unless the polish bar demands a SPA.
2. **FinProxy access model** — Tailscale (simplest, but asks non-technical
   founders to install it) vs a hosted authenticated view (more work, better UX,
   aligns with hosted-self-serve). 
3. **Live vs replay boundary** — what is live (WebSocket) vs JetStream-replayed
   (ledger history); read-model storage (in-memory projection vs a small read DB).
4. **Tenant→view mapping** — how a logged-in user resolves to a NATS account /
   scope; the isolation boundary.
5. **Seam for the PO panel** — reserve the structure so James's interactive door
   folds in later without a rewrite.

---

## Open questions for /system-design to resolve

1. **Event→view projection** — which topics feed which panels (fleet health,
   work-in-flight, build status, deploy/verify status, delivery ledger).
2. **Read-model schema** — projection of NATS events → dashboard view models.
3. **Ledger query** — JetStream replay → "delivered this period for {tenant}"
   with PR links.
4. **Auth + tenant scoping** mechanism (NATS account resolution per session).
5. **Push contract** — WebSocket/SSE to the frontend.
6. **Approve-action publish contract (later)** — cross-ref the output-loop gate
   and the Slack approve-to-build gate; confirm one shared mechanism.

---

## Hardware / topology

| Machine | Role |
|---|---|
| GB10 (2× DGX Spark) | Dashboard backend (FastAPI + `nats-core`); NATS JetStream (live + replay) |
| Tailscale | Rich's access; FinProxy access model per `/system-arch` |
| AWS (later) | Optional client-facing deployment — low-risk (Docker Compose), not a v1 prerequisite |

---

## Repo structure (target — `/system-design` finalises)

```
factory-dashboard/
├── backend/
│   ├── app.py                 ← FastAPI entrypoint
│   ├── nats_consumer.py       ← subscribes via nats-core; live projection
│   ├── projections/           ← event → view-model projections per panel
│   ├── ledger.py              ← JetStream replay → delivered-this-period
│   └── auth.py                ← session → tenant/NATS-scope resolution
├── frontend/                  ← per the /system-arch stack decision
└── tests/
```

---

## Key insight to carry forward

**The dashboard is a projection, not a participant.** It reads the bus Rich
already owns and renders two views from it — Rich's operational view and
FinProxy's commercial ledger. Building it adds no orchestration and no
dependency; it makes the work **visible**, and for FinProxy it makes the
work-as-debt **legible**. The only write it will ever make is the same
approve-gate action that the deploy loop and the Slack door already use — one
approval surface across the whole system.

---

*Prepared: 19 June 2026 | own the spine → delivery read-model*
*Use as context for /system-arch and /system-design. Companions: factory-scaling-and-output-bottleneck-findings.md, fleet-gateway-slack-jarvis-door-conversation-starter.md*
