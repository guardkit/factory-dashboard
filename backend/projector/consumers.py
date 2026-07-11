"""nats-core consumers (projector connection A + one per client-tenant registry row).

STAGE-OWNED: built at S2 (D1). Empty at S1 by fence 1 — NO NATS import exists anywhere in the D0
scaffold. When built, ALL subscriptions are plain core-NATS (zero JetStream API, zero consumers on
PIPELINE, zero publish call sites — ADR-DASH-007 / M-D3); `$KV.agent-registry.>` is a subject-space
read, never a KV/JS API call (capability note drift 3).
"""
