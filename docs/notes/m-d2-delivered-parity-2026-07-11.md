# M-D2 — delivered-ledger parity judgment (S3 close)

**Date:** 2026-07-11 · **Stage:** S3 (D2) · **Measure:** M-D2 (build-plan §2) — does the query
layer's `delivered_period` agree with a manual sweep of the authoritative delivery feed?

**Method (read-only, in-session):** ran `ledger.bootstrap_from_forge` against the REAL forge-prod
DB opened URI `mode=ro` (fence 4) — `/home/richardwoollcott/forge-prod-state/.forge/forge.db`, the
forge-prod bind-mount, NOT the `~/.forge` dev default (capability note drift 1) — into a throwaway
read-model, then read `dbread.delivered_panel` over a deliberately wide window (2026-01-01 →
2026-12-31, to catch every delivery). Cross-checked against a hand SQL sweep of forge `builds` and
an attempted `gh` merge-boundary check. No writes to either store; the forge handle was closed
before any read-model write (never co-holds both stores — F-2g).

## Manual sweep (forge-prod `builds`, mode=ro)

| status | count |
|---|---|
| COMPLETE | **1** |
| CANCELLED | 26 |
| INTERRUPTED | 25 |
| FAILED | 5 |
| PAUSED | 1 |

The single COMPLETE build:

| feature_id | repo | branch | completed_at | pr_url | merge_sha |
|---|---|---|---|---|---|
| FEAT-9E59 | appmilla/api_test | ddd-demo | 2026-07-04T08:58:13Z | *(none)* | *(no column)* |

Receipt-bearing COMPLETE builds (the delivered bar's evidence, amended DDR-DASH-001): **0.**
`pr_url` is empty on the one COMPLETE, and the forge `builds` schema has **no `merge_sha`
column at all** (verified prod DDL) — so no forge-bootstrapped build can carry a receipt today.

## `delivered_period` result (query layer)

- `delivered_count` = **0**
- rows rendered = **1**, and it is the FEAT-9E59 **"complete — merge unverified" named gap**
  (`change_verified=False`), NOT a delivered row.

## Verdict: 100% agreement

`delivered_period` (0 delivered features) == the manual receipt-bearing COMPLETE count (0). The
lone COMPLETE is surfaced honestly as a merge-unverified gap, never counted as a delivery. **This
is the measure working, not a discrepancy** — it is exactly the zero-receipt launch reality the
spec pre-registered (ux §4.4 "honestly near-empty at launch"; design §5 P7 addendum + capability
note drift 2: `pr_url` has never been populated in any forge DB to date; the Delivered page fills
as forge-daemon builds complete with receipts, WS2-V1, w/c 07-14).

## Named limitations (recorded honestly, not improvised around)

1. **gh merge-boundary cross-check not resolvable.** `gh` is authenticated
   (account RichWoollcott, scopes incl. `repo`) but the target repo `appmilla/api_test` returns
   *"Could not resolve to a Repository"* — it is a pre-daemon guardkit demo target (branch
   `ddd-demo`, project unset), not a live client repo, so the merge boundary cannot be
   independently verified via GitHub. This does not affect the parity result: the forge feed is
   authoritative for the ledger, and it carries no receipt, so `delivered=0` stands regardless.
2. **Pre-daemon deliveries are out of scope by design.** Any real deliveries that predate the
   forge daemon live only in guardkit artifacts (unscraped — design §5 / capability note). They do
   not appear in forge `builds`, so they are neither counted nor claimed; M-D2 names this as the
   feed gap it is rather than fabricating rows.

## Re-runnable

The judgment is reproducible read-only:

```
FACTORY_FORGE_DB_PATH=/home/richardwoollcott/forge-prod-state/.forge/forge.db \
  <bootstrap into a temp read-model> ; delivered_panel(window=("2026-01-01","2026-12-31"))
```

The offline M-D2 parity batch job (design §7 F-2g, its own read-only credential) will write the
stored value the `/delivered` footer displays; the request path never runs this sweep. Until that
job runs, the footer honestly reads "M-D2 parity: not yet run" (display-only).
