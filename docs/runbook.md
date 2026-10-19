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
`./data/readmodel.db` is disposable: if it is deleted, the feed rebuilds it on its next pass.
`./data/suggestions.db` is not: it holds the suggestions people have sent (below), and nothing
rebuilds it. Delete only `readmodel.db` (and its `-wal` and `-shm` files), never the whole folder.

For the Linear door (18 October 2026): after each pass the feed also rewrites
`./data/project-summaries.json` (each project's delivered, open and cost figures and health, as
the pages show them; the door posts them to Linear after a merge). It is rebuilt every 15 seconds,
in its own loop beside the copy of Forge's record.
The board shows each project's Linear link from the door's own volume
(`forge-estate_linear-door-state`, or `FACTORY_LINEAR_DOOR_VOLUME` in the shell or `./.env`; the
script asks compose for the name, `deploy/linear-volume.sh`), mounted read-only by
`docker-compose.linear.yml`. `deploy/dashboard-up.sh` adds that file only when the volume exists,
because compose refuses to start anything when an external volume is missing; without it the pages
simply show no Linear links. Run the script again once the door's volume exists.

## Start, or bring it back after a rollout or reboot

```sh
deploy/dashboard-up.sh            # start with the current image
deploy/dashboard-up.sh --build    # rebuild from this checkout first (after a code change)
```

It starts whatever is stopped, waits for the feed's first read, and prints the result. Exit
status 0 means the dashboard is reading the factory's record. It needs the board's configuration
folder (`~/.config/factory-dashboard/` with `session_secret` and `accounts.yaml`, or the folder in
`FACTORY_DASH_CONFIG_DIR`; see the README's "Accounts and access"); without it, it starts nothing
and exits 2 with one sentence saying which file is missing.

The containers use `restart: unless-stopped`, so a reboot brings them back on its own. What left
the dashboard stopped on 1 October was a rollout step (`docker compose down` or a stop) with no
matching start. So:

- **The factory's start script** runs `deploy/dashboard-up.sh` as its last line, after the estate
  is up. It never fails the factory's start: run it as `deploy/dashboard-up.sh || true`.
- `/healthz` needs no sign-in; every page does.
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

## Suggestions and "What changed"

Operators can send a suggestion for the dashboard from the "Suggest a change" link at the foot of
every page (text, or a voice note turned into text). The Suggestions page (`/suggestions`) lists
them with who sent each, when, from which page, and its status (new, seen, done or declined); any
operator can set the status there. They are kept in `./data/suggestions.db`, the only file the
board writes while it runs; the feed never opens it. Each person can send 10 an hour, each up to
2,000 characters, and record 10 voice notes an hour.

If `suggestions.db` is locked or damaged, the board still starts (the log says
"suggestions.db cannot be opened"), every other page works, and the suggestion pages say in one
sentence that suggestions can't be saved or read just now. To start afresh, stop the board, move
the damaged file aside and start it again; it creates an empty one.

To read them without a browser (read-only; it never creates the file):

```sh
docker compose exec board python -m backend.suggestions list               # newest first
docker compose exec board python -m backend.suggestions list --status new  # only the new ones
docker compose exec board python -m backend.suggestions list --json        # for a script
# or, from this checkout without the container:
.venv/bin/python -m backend.suggestions list --data ./data
```

The "What changed" page (`/changes`) shows `CHANGES.md` from this repository: a dated list in
plain words, newest first. Add a line under its day (a `## 18 October 2026` heading, then
`- what changed`) when a change goes live; the page shows only those headings and lines, as plain
text. The image carries the file, so a new line shows after `deploy/dashboard-up.sh --build`.
