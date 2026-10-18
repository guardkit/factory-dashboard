"""On track: the thresholds (backend/config/norms.yaml) and each feature's signals against them.

The July thresholds were set when a feature took 6–9 attempts. Design requirement 7 re-derives
them from recent builds and records them with a date. `derive` does that from the dashboard's own
copy of Forge's record, so the numbers can be re-derived the same way later:

    python -m backend.readmodel.norms --db data/readmodel.db --days 14

prints a norms.yaml to review and commit.

A signal with no data is shown as a gap ("not measured", with the reason), never as green.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from backend.readmodel.factory import (
    Factory,
    Feature,
    Outcome,
    build_seconds,
    load_factory,
    planning_seconds,
)

NORMS_PATH = Path(__file__).resolve().parent.parent / "config" / "norms.yaml"

GREEN, AMBER, RED, GAP = "G", "A", "R", "—"
_RANK = {GREEN: 0, GAP: 0, AMBER: 1, RED: 2}


@dataclass(frozen=True)
class Band:
    green_max: float
    amber_max: float
    unit: str
    how: str

    def band(self, value: float) -> str:
        if value <= self.green_max:
            return GREEN
        if value <= self.amber_max:
            return AMBER
        return RED

    def describe(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        g, a = _num(self.green_max), _num(self.amber_max)
        return f"green up to {g}{unit}, amber up to {a}{unit}, red above"


@dataclass(frozen=True)
class Norms:
    derived_on: str
    window_days: int
    attempts: Band
    planning_minutes: Band
    build_minutes: Band
    stall_minutes: Band
    waiting_hours: Band
    quiet_hours: Band

    @property
    def source_line(self) -> str:
        return f"Thresholds re-derived on {self.derived_on} from the last {self.window_days} days of builds."


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _band(raw: dict[str, Any], name: str) -> Band:
    entry = raw[name]
    return Band(float(entry["green_max"]), float(entry["amber_max"]), str(entry["unit"]), str(entry["how"]).strip())


def load_norms(path: Path = NORMS_PATH) -> Norms:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Norms(
        derived_on=str(raw["derived_on"]), window_days=int(raw["window_days"]),
        attempts=_band(raw, "attempts"), planning_minutes=_band(raw, "planning_minutes"),
        build_minutes=_band(raw, "build_minutes"), stall_minutes=_band(raw, "stall_minutes"),
        waiting_hours=_band(raw, "waiting_hours"), quiet_hours=_band(raw, "quiet_hours"),
    )


# --- one feature's signals ------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    name: str
    value: str          # "2", "34 min", "—"
    band: str           # G | A | R | —
    norm: str           # "green up to 1, amber up to 2, red above"
    note: str = ""      # why it is not measured, or what the value means


@dataclass(frozen=True)
class OnTrack:
    band: str           # the worst measured band; R for the factory's own failures; — otherwise
    words: str          # "On track", "At risk", "Off track", "Not measured yet", or how it ended
    signals: tuple[Signal, ...]
    reason: str = ""    # one line: what drove the colour


_WORDS = {GREEN: "On track", AMBER: "At risk", RED: "Off track", GAP: "Not measured yet"}


def _mins(seconds: float) -> str:
    m = round(seconds / 60)
    if m >= 48 * 60:
        return f"{m // 1440} days {m % 1440 // 60} h"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def on_track(f: Feature, norms: Norms, now: datetime) -> OnTrack:
    signals: list[Signal] = []

    if f.attempts:
        signals.append(Signal("Build attempts", str(f.attempts), norms.attempts.band(f.attempts),
                              norms.attempts.describe(), "each attempt is one build of this feature"))
    else:
        signals.append(Signal("Build attempts", "—", GAP, norms.attempts.describe(), "no build yet"))

    p = planning_seconds(f, now)
    if p is None:
        why = ("no planning run: it was sent already planned" if f.builds
               else "planning never started" if f.planning is None
               else "planning has not started yet")
        signals.append(Signal("Planning time", "—", GAP, norms.planning_minutes.describe(), why))
    else:
        so_far = " so far" if f.planning and f.planning.active else ""
        signals.append(Signal("Planning time", _mins(p) + so_far, norms.planning_minutes.band(p / 60),
                              norms.planning_minutes.describe(), "start to finish, not counting approval waits"))

    b = build_seconds(f, now)
    if b is None:
        signals.append(Signal("Build time", "—", GAP, norms.build_minutes.describe(), "no build has started"))
    else:
        latest = f.latest_build
        so_far = " so far" if latest and latest.completed_at is None else ""
        signals.append(Signal("Build time", _mins(b) + so_far, norms.build_minutes.band(b / 60),
                              norms.build_minutes.describe(), "latest attempt, not counting approval waits"))

    signals.append(_stall(f, norms, now))

    longest = max((w.seconds(now) for w in f.waits), default=None)
    if longest is None:
        # Never reached a gate: there was nothing to wait for, which is not the same as "no wait".
        signals.append(Signal("Waiting for a person", "—", GAP, norms.waiting_hours.describe(),
                              "never reached an approval gate"))
    else:
        current = f.open_wait()
        note = "waiting now" if current is not None else "the longest single wait"
        signals.append(Signal("Waiting for a person", _mins(longest), norms.waiting_hours.band(longest / 3600),
                              norms.waiting_hours.describe(), note))

    sigs = tuple(signals)
    # A feature that finished without being delivered is not "on track" or otherwise: say how it ended.
    if f.outcome in OUTCOME_WORDS:
        band, words = OUTCOME_WORDS[f.outcome]
        return OnTrack(band, words, sigs, f.status_text)
    measured = [s for s in signals if s.band != GAP]
    if not measured:
        return OnTrack(GAP, _WORDS[GAP], sigs, "nothing has been measured for this feature yet")
    worst = max((s.band for s in measured), key=lambda b: _RANK[b])
    reason = ""
    if worst in (AMBER, RED):
        reason = "; ".join(f"{s.name.lower()} {s.value}" for s in measured if s.band == worst)
    return OnTrack(worst, _WORDS[worst], sigs, reason)


#: Finished without being delivered: the outcome is shown in place of an on-track colour. The
#: factory's own failures stay red; the rest (a person's decision, a cancellation, a clean-up) are
#: shown without a colour, because they say nothing about whether the factory was on track.
OUTCOME_WORDS: dict[Outcome, tuple[str, str]] = {
    Outcome.BUILD_FAILED: (RED, "Build failed"),
    Outcome.MERGE_FAILED: (RED, "Merge check failed"),
    Outcome.PLANNING_FAILED: (RED, "Planning failed"),
    Outcome.CANCELLED: (GAP, "Cancelled"),
    Outcome.PLANNING_STOPPED: (GAP, "Planning stopped"),
    Outcome.NOT_MERGED: (GAP, "Built, not merged"),
    Outcome.MISSION_NOT_MERGED: (GAP, "Mission not merged"),
    Outcome.BUILT_NO_MERGE: (GAP, "Built, no merge recorded"),
    Outcome.STOPPED_IN_QUEUE: (GAP, "Stopped in the queue"),
    Outcome.RETIRED: (GAP, "Retired"),
    Outcome.NOTHING_TO_BUILD: (GAP, "Already done"),
    Outcome.HANDED_OFF: (GAP, "Handed over"),
}


@dataclass(frozen=True)
class Quiet:
    """How long an unfinished feature has gone with nothing recorded, against its threshold."""

    since: datetime
    seconds: float
    band: str
    norm: Band
    planning: bool      # True: a working planning run, measured against the planning stall threshold


def quiet_spell(f: Feature, norms: Norms, now: datetime) -> Quiet | None:
    """The silence since the last thing Forge recorded for a feature that is still in progress and
    is not waiting for a person. A working planning run is measured against the planning stall
    threshold; anything else (in the queue, waiting for its build, building, merging) against the
    longer "nothing recorded" threshold, because Forge records nothing inside a build. A long
    silence never ends a feature: it stays in progress, shown as stalled."""
    if f.outcome is not Outcome.IN_PROGRESS or f.open_wait() is not None:
        return None
    last = f.last_activity_at()
    if last is None:
        return None
    secs = max(0.0, (now - last).total_seconds())
    run = f.planning
    if not f.builds and run is not None and run.active and run.state != "QUEUED":
        return Quiet(last, secs, norms.stall_minutes.band(secs / 60), norms.stall_minutes, True)
    return Quiet(last, secs, norms.quiet_hours.band(secs / 3600), norms.quiet_hours, False)


def _stall(f: Feature, norms: Norms, now: datetime) -> Signal:
    name, norm = "No activity while running", norms.stall_minutes.describe()
    if not f.in_progress:
        return Signal(name, "—", GAP, norm, "finished")
    if f.open_wait() is not None:
        return Signal(name, "—", GAP, norm, "waiting for a person, not running")
    q = quiet_spell(f, norms, now)
    if q is not None and q.planning:
        return Signal(name, _mins(q.seconds), q.band, q.norm.describe(), "since the last planning step")
    if q is not None and q.band != GREEN:
        return Signal(name, _mins(q.seconds), q.band, q.norm.describe(),
                      "nothing at all recorded for this feature since then; it is still in progress in "
                      "the factory's record")
    if f.builds:
        return Signal(name, "—", GAP, norm,
                      "not measured during a build: the factory records nothing between a build's start and finish, "
                      "so build time is the signal for a long build")
    return Signal(name, "—", GAP, norm, "not running yet: waiting in the queue, for planning or for its build")


# --- re-deriving the thresholds -----------------------------------------------------------------


def _pct(values: list[float], p: float) -> float:
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _planning_gaps_minutes(f: Feature) -> list[float]:
    """Gaps between consecutive planning events while a run was working (not waiting)."""
    run = f.planning
    if run is None or run.started_at is None or run.completed_at is None:
        return []
    waits = [w for w in f.waits if w.kind == "spec approval"]
    gaps = []
    prev = None
    for e in run.events:
        if not (run.started_at <= e.at <= run.completed_at):
            continue
        if prev is not None and not any(w.started_at <= prev < (w.ended_at or e.at) for w in waits):
            gaps.append((e.at - prev).total_seconds() / 60)
        prev = e.at
    return gaps


def derive(factory: Factory, now: datetime, days: int = 14) -> dict[str, Any]:
    """Thresholds from the features of the last `days` days, as a norms.yaml mapping."""
    since = now - timedelta(days=days)
    recent = [f for f in factory.features if f.sent_at and f.sent_at >= since]
    plans = [s / 60 for f in recent
             if f.planning and f.planning.completed_at and f.planning.state in ("BUILD_QUEUED", "PLANNED_HANDOFF")
             and (s := planning_seconds(f, now)) is not None]
    builds = [s / 60 for f in recent
              if f.latest_build and f.latest_build.status == "COMPLETE" and (s := build_seconds(f, now)) is not None]
    gaps = [g for f in recent for g in _planning_gaps_minutes(f)]
    attempts = [f.attempts for f in recent if f.attempts]
    two_plus = sum(1 for a in attempts if a >= 2)
    date = now.date().isoformat()

    def r(x: float) -> int:
        return round(x)

    return {
        "derived_on": date,
        "window_days": days,
        "attempts": {
            "green_max": 1, "amber_max": 2, "unit": "",
            "how": f"Kept from the design: one build per request is normal. Of the {len(attempts)} features "
                   f"built in the {days} days to {date}, {two_plus} needed two or more attempts.",
        },
        "planning_minutes": {
            "green_max": r(_pct(plans, 0.75)), "amber_max": r(_pct(plans, 0.95)), "unit": "min",
            "how": f"75th and 95th percentile of planning time (start to finish, minus approval waits) of the "
                   f"{len(plans)} planning runs that finished in the {days} days to {date}.",
        },
        "build_minutes": {
            "green_max": r(_pct(builds, 0.75)), "amber_max": r(_pct(builds, 0.95)), "unit": "min",
            "how": f"75th and 95th percentile of build time (start to finish of the latest attempt, minus "
                   f"approval waits) of the {len(builds)} builds that finished in the {days} days to {date}.",
        },
        "stall_minutes": {
            "green_max": 30, "amber_max": 60, "unit": "min",
            "how": f"Kept from the design. In the {days} days to {date} the longest quiet spell inside a "
                   f"working planning run was {r(max(gaps, default=0))} min and 99% were under "
                   f"{r(_pct(gaps, 0.99)) if gaps else 0} min ({len(gaps)} gaps), so over 30 min is unusual and "
                   f"over an hour is a stall. Only planning can be checked: Forge records nothing inside a build.",
        },
        "waiting_hours": {
            "green_max": 1, "amber_max": 4, "unit": "h",
            "how": "Kept from the July design (under 1 h normal, over 4 h needs chasing); waiting is a person's "
                   "time, not the factory's, so recent builds do not change it.",
        },
        "quiet_hours": {
            "green_max": 4, "amber_max": 24, "unit": "h",
            "how": f"For a feature still in progress that is not planning and not waiting for a person: how long "
                   f"nothing at all has been recorded for it. Forge records nothing inside a build, and the "
                   f"95th percentile build in the {days} days to {date} took {r(_pct(builds, 0.95))} min, so up "
                   f"to 4 h of silence is normal and over a day the record has stopped moving. The feature "
                   f"stays in progress, shown as stalled, until Forge records how it ended.",
        },
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover (operator tool)
    ap = argparse.ArgumentParser(description="Re-derive the on-track thresholds from the dashboard's store.")
    ap.add_argument("--db", required=True, type=Path, help="the dashboard store (readmodel.db)")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        now = datetime.now(UTC)
        data = derive(load_factory(conn, now), now, args.days)
    finally:
        conn.close()
    sys.stdout.write("# On-track thresholds for the factory dashboard. Re-derive with:\n")
    sys.stdout.write("#   python -m backend.readmodel.norms --db data/readmodel.db --days 14\n")
    sys.stdout.write(yaml.safe_dump(data, sort_keys=False, width=100, allow_unicode=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
