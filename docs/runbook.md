# Running the factory dashboard

Written 18 October 2026 (part A of the
[October design](architecture/live-data-cost-time-voice-design-2026-10-17.md)).

## What runs

Two containers from one image, under `docker compose` in this checkout:

- **board**: the web pages on `127.0.0.1:8090` (loopback only).
- **projector** ("the feed"): copies Forge's build record into `./data/readmodel.db` every
  15 seconds, read-only, and checks the model gateway, the model server and the bus monitor every
  30 seconds. It has no bus connection and needs no NATS login.

Forge's record is the estate's ledger volume (`forge-estate_forge-ledger`), mounted read-only.
`./data` is disposable: if it is deleted, the feed rebuilds it on its next pass.

## Start, or bring it back after a rollout or reboot

```sh
deploy/dashboard-up.sh            # start with the current image
deploy/dashboard-up.sh --build    # rebuild from this checkout first (after a code change)
```

It starts whatever is stopped, waits for the feed's first read, and prints the result. Exit
status 0 means the dashboard is reading the factory's record.

The containers use `restart: unless-stopped`, so a reboot brings them back on its own. What left
the dashboard stopped on 1 October was a rollout step (`docker compose down` or a stop) with no
matching start. So:

- **The factory's start script** runs `deploy/dashboard-up.sh` as its last line, after the estate
  is up. It never fails the factory's start: run it as `deploy/dashboard-up.sh || true`.
- **The release runbook's final checks** include: `curl -s http://127.0.0.1:8090/healthz` shows
  `"factory_record":"fresh"` and `"feed":"running"`, and the dashboard's pages show no red banner.

(The start script and the release runbook are private operations files; this section says what
they contain.)

## Checking it

```sh
curl -s http://127.0.0.1:8090/healthz
# {"factory_record":"fresh","feed":"running","last_read_at":"2026-10-18T10:52:33+00:00"}
```

- `factory_record` is `fresh` when the last good read is under two minutes old, `stale` after
  that, and `never read` before the first read.
- `feed` is `stopped` when the feed's heartbeat is over a minute old.

## The red banner

Every page shows one red line when its data may be out of date:

- "Factory data last read at 08:14 — the dashboard cannot reach the factory's record": the feed
  is running but cannot open Forge's database. Usual cause: Forge is stopped (a read-only reader
  cannot open SQLite's write-ahead log while nobody holds it open). It clears within 15 seconds of
  Forge starting. The Fleet page shows the exact error.
- "The dashboard's data feed is not running": the projector container has stopped. Run
  `deploy/dashboard-up.sh`.

The pages keep showing the last known data, with the time it was read.

## On-track thresholds

`backend/config/norms.yaml` holds the thresholds and how each was derived. Re-derive them from
recent builds with

```sh
docker compose exec projector python -m backend.readmodel.norms --db /data/readmodel.db --days 14
```

and commit the result with its date.
