"""The dashboard's summary file for the Linear door (design "Linear front door", section 11.5,
18 October 2026): ``/data/project-summaries.json``, beside the store.

The door posts a short project update to Linear after each merge. The dashboard computes the
figures and the door only formats them, so every figure here comes from the functions the pages
use, on ONE read snapshot of the store (`dbread.snapshot`) passed as ``conn=`` to each view: per
repository `project_view` (the project page), the repository list from `projects_view` (the
Projects page). Main's scope rule is kept: this is the operator's whole-factory view.

The feed writes it every 15 s (the Forge copy's own cadence) in its own loop, `summary_loop`, so a
slow summary never delays the copy of Forge's record. Each write is atomic: a temporary file in the
same directory, then `os.replace`, so a reader sees the old file or the new one, never half of one.
The board never writes it.

Contract, version 1::

    {"version": 1, "written_at": iso,
     "forge": {"last_success_at": iso|null, "stale": bool, "valid_until": iso|null},
     "cost":  {"read_at": iso|null, "state": "fresh|stale|never", "stale_after_seconds": 600},
     "projects": {"owner/name": {"delivered_total", "delivered_7d", "in_progress",
                                 "open_issues_total", "open_issues_words", "health",
                                 "spend_total": SpendSlot, "spend_week": SpendSlot}},
     "attempts": {"lin-...": SpendSlot},  # the attempt's merge cost
     "merges": {"<build id>": SpendSlot}}  # the cost of each build that reached main (11.8)

``forge.valid_until`` is the moment the pages would call Forge's evidence stale: the earlier of the
last good read plus its limit and the feed's heartbeat plus its limit (`dbread.Freshness.fresh`), so
the door can judge freshness when it reads. A SpendSlot is every field of the dashboard's
`SpendSlot`, so ``SpendSlot(**d)`` rebuilds it.

**Merges** (design 11.8, 19 October 2026). One slot per build that reached main in the last 30
days, whatever started it, keyed by build id: "on main" is `Build.on_main`, the publication-record
predicate the Linear door and Forge's mission finish use. A single feature's merge costs the
deduplicated union of that feature's tags (queue, planning, builds). A mission's merge build costs
every part of the mission, built or not (`mission_parts`), plus the mission's own tags, with the
same unmeasured and missing rules as `attempts` (`dbread.attempt_spend`).

**Health.** The worst on-track band of the project's in-progress features, taken from the project
page's own rows (so the same `norms.on_track` function and thresholds, nothing new), mapped in
`HEALTH_OF_BAND`. Features whose band is "not measured yet" are left out; if every in-progress
feature is unmeasured the health is "unknown"; with nothing in progress it is "on_track" (nothing
is running late). When the Forge evidence is stale the health is "unknown": missing evidence is
never shown as on track.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from backend.projector.forge_mirror import MIRROR_INTERVAL_SECS
from backend.projector.store import iso
from backend.readmodel import dbread
from backend.readmodel import viewmodels as vm
from backend.readmodel.costs import COST_STALE_SECS, cost_freshness, feature_tags, load_cost_book
from backend.readmodel.factory import Factory, Feature, Mission, load_factory
from backend.readmodel.norms import _RANK, AMBER, GREEN, RED

SUMMARY_FILE = "project-summaries.json"
VERSION = 1

#: The pages' per-feature on-track bands (norms.py: G, A, R; "—" is "not measured yet") as the
#: summary's health words. The one place this mapping lives.
HEALTH_OF_BAND = {GREEN: "on_track", AMBER: "at_risk", RED: "off_track"}

#: A Linear-originated attempt's correlation id starts with this (the door's ids).
LINEAR_PREFIX = "lin-"
#: Attempts last seen more than this long ago are left out.
ATTEMPT_DAYS = 30
#: Builds that reached main more than this long ago are left out of `merges`.
MERGE_DAYS = 30


def summary_path(db_path: Path) -> Path:
    return db_path.parent / SUMMARY_FILE


def _iso(dt: datetime | None) -> str | None:
    return iso(dt) if dt is not None else None


def slot_dict(slot: vm.SpendSlot) -> dict[str, Any]:
    return dataclasses.asdict(slot)


def project_health(view: vm.ProjectView, forge_stale: bool) -> str:
    if forge_stale:
        return "unknown"
    bands = [r.band.band for r in view.features.rows if r.in_progress]
    if not bands:
        return "on_track"
    measured = [b for b in bands if b in HEALTH_OF_BAND]
    if not measured:
        return "unknown"
    return HEALTH_OF_BAND[max(measured, key=lambda b: _RANK[b])]


def _last_seen(features: list[Feature], mission: Mission | None) -> datetime | None:
    times = [t for f in features for t in (f.sent_at, f.last_activity_at(), f.ended_at, f.delivered_at) if t]
    if mission is not None:
        times += [t for t in (mission.created_at, mission.closed_at, mission.delivered_at()) if t]
    return max(times, default=None)


def _mission_by_prefix(f: Feature, missions: dict[str, Mission]) -> Mission | None:
    """The mission of `missions` (keyed by correlation id; any missions, Linear or not) a feature is
    a part of. Forge names each part's attempt `<mission correlation id>-<KEY>-a<n>` (planning run,
    queue item and build alike), and only a part that reached a build carries the mission's id, so
    a part that stopped in planning is found by that prefix. The match is on a boundary only: a tag
    equals the mission's id, or starts with the id followed by "-" (so `m-abcd-K2-a1` is never a
    part of `m-abc`). When two missions' ids both match a tag, the longer (the more exact) wins."""
    if f.mission is not None and f.mission.correlation_id in missions:
        return f.mission
    found: Mission | None = None
    for tag in feature_tags(f):
        for corr, m in missions.items():
            if (corr and (tag == corr or tag.startswith(corr + "-"))
                    and (found is None or len(corr) > len(found.correlation_id))):
                found = m
    return found


def mission_parts(factory: Factory) -> dict[str, list[Feature]]:
    """Every mission's parts, keyed by mission id: the features whose builds carry the mission's id
    and those found by its correlation prefix (failed and planning-only parts included)."""
    by_corr = {m.correlation_id: m for m in factory.missions if m.correlation_id}
    parts: dict[str, list[Feature]] = {m.id: [] for m in factory.missions}
    for f in factory.features:
        mission = f.mission if f.mission is not None else _mission_by_prefix(f, by_corr)
        if mission is not None:
            parts.setdefault(mission.id, []).append(f)
    return parts


def merges_on_main(factory: Factory, now: datetime) -> dict[str, tuple[Mission | None, list[Feature]]]:
    """Builds on main (`Build.on_main`) that reached it in the last 30 days, by build id: a
    mission's merge build with its mission and every part, or a feature's build with that feature.
    A build whose record says nothing of when is kept (the door asks for it by id)."""
    cut = now - timedelta(days=MERGE_DAYS)
    by_merge = {m.merge_build.build_id: m for m in factory.missions if m.merge_build is not None}
    owner = {b.build_id: f for f in factory.features for b in f.builds}
    parts = mission_parts(factory)
    out: dict[str, tuple[Mission | None, list[Feature]]] = {}
    for b in factory.builds:
        if not b.on_main:
            continue
        reached = b.reached_main_at()
        if reached is not None and reached < cut:
            continue
        if b.is_mission_merge:
            mission = by_merge.get(b.build_id)
            out[b.build_id] = (mission, parts.get(mission.id, []) if mission is not None else [])
        else:
            f = owner.get(b.build_id)
            out[b.build_id] = (None, [f] if f is not None else [])
    return dict(sorted(out.items()))


def linear_attempts(factory: Factory, now: datetime) -> dict[str, tuple[Mission | None, list[Feature]]]:
    """Linear-originated attempts seen in the last 30 days: a mission (``lin-m-…``) with every part
    on record (built or not), or a single issue (``lin-<identifier>-<n>``) with the feature whose
    queue, planning or build ids carry it."""
    missions = {m.correlation_id: m for m in factory.missions if m.correlation_id.startswith(LINEAR_PREFIX)}
    found: dict[str, tuple[Mission | None, list[Feature]]] = {corr: (m, []) for corr, m in missions.items()}
    for f in factory.features:
        mission = _mission_by_prefix(f, missions)
        if mission is not None:
            found[mission.correlation_id][1].append(f)
            continue
        if f.mission is not None:
            continue  # a part of a mission that did not come from Linear
        for tag in sorted(t for t in feature_tags(f) if t.startswith(LINEAR_PREFIX)):
            single, features = found.setdefault(tag, (None, []))
            if single is None and f not in features:
                features.append(f)
    cut = now - timedelta(days=ATTEMPT_DAYS)
    return {k: v for k, v in sorted(found.items())
            if (seen := _last_seen(v[1], v[0])) is not None and seen >= cut}


def build_summary(db_path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    conn = dbread.snapshot(db_path)
    try:
        fr = dbread.freshness(conn)
        stale = not fr.fresh(now)
        valid_until = None
        if fr.read_at is not None and fr.heartbeat_at is not None:
            valid_until = min(fr.read_at + timedelta(seconds=vm.STALE_AFTER_SECS),
                              fr.heartbeat_at + timedelta(seconds=vm.PROJECTOR_STALE_SECS))
        cf = cost_freshness(conn)
        projects: dict[str, Any] = {}
        for row in dbread.projects_view(db_path, now, conn=conn).rows:
            view = dbread.project_view(db_path, row.project, now, conn=conn)
            projects[row.project] = {
                "delivered_total": view.delivered_total, "delivered_7d": view.delivered_7d,
                "in_progress": view.in_progress, "open_issues_total": view.open_issues_total,
                "open_issues_words": view.open_issues_words, "health": project_health(view, stale),
                "spend_total": slot_dict(view.spend), "spend_week": slot_dict(view.spend_week),
            }
        factory, book = load_factory(conn, now), load_cost_book(conn, now)
        attempts = {corr: slot_dict(dbread.attempt_spend(book, features, mission))
                    for corr, (mission, features) in linear_attempts(factory, now).items()}
        merges = {bid: slot_dict(dbread.attempt_spend(book, features, mission, scope="merge"))
                  for bid, (mission, features) in merges_on_main(factory, now).items()}
    finally:
        conn.close()
    return {
        "version": VERSION, "written_at": iso(now),
        "forge": {"last_success_at": _iso(fr.read_at), "stale": stale, "valid_until": _iso(valid_until)},
        "cost": {"read_at": _iso(cf.read_at), "state": cf.state(now), "stale_after_seconds": COST_STALE_SECS},
        "projects": projects, "attempts": attempts, "merges": merges,
    }


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    """A temporary file in the same directory, then `os.replace`: a reader never sees half a file.
    Mode 0644, so the door (another account, a read-only mount) can read it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_summary(db_path: Path, now: datetime | None = None, out: Path | None = None) -> dict[str, Any]:
    data = build_summary(db_path, now)
    write_atomic(out or summary_path(db_path), data)
    return data


def remove_leftovers(directory: Path) -> None:
    """Temporary files a write left behind when the feed was stopped mid-write (at start only)."""
    for tmp in directory.glob(f".{SUMMARY_FILE}.*.tmp"):
        with contextlib.suppress(OSError):
            tmp.unlink()


class _Writer(Protocol):
    def __call__(self, db_path: Path, now: datetime | None = ...) -> object: ...


async def summary_loop(db_path: Path, *, interval: float = MIRROR_INTERVAL_SECS,
                       write: _Writer = write_summary) -> None:
    """Its own loop beside the Forge copy, in a thread, so a slow summary never delays a Forge pass.
    A failed write is skipped (the file then ages, and the door sees it by `valid_until`)."""
    while True:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(write, db_path, datetime.now(UTC))
        await asyncio.sleep(interval)
