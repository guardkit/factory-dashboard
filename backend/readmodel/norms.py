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
    FAILURES,
    Factory,
    Feature,
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
        waiting_hours=_band(raw, "waiting_hours"),
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
    band: str           # the worst measured band, or R when the factory itself failed
    words: str          # "On track", "At risk", "Off track", "Not measured yet"
    signals: tuple[Signal, ...]
    reason: str = ""    # one line: what drove the colour


_WORDS = {GREEN: "On track", AMBER: "At risk", RED: "Off track", GAP: "Not measured yet"}


def _mins(seconds: float) -> str:
    m = round(seconds / 60)
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
        signals.append(Signal("Planning time", "—", GAP, norms.planning_minutes.describe(),
                              "no planning run: it was sent already planned"))
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
        signals.append(Signal("Waiting for a person", "none", GREEN, norms.waiting_hours.describe(),
                              "no approval wait recorded"))
    else:
        current = f.open_wait()
        note = "waiting now" if current is not None else "the longest single wait"
        signals.append(Signal("Waiting for a person", _mins(longest), norms.waiting_hours.band(longest / 3600),
                              norms.waiting_hours.describe(), note))

    measured = [s for s in signals if s.band != GAP]
    worst = max((s.band for s in measured), key=lambda b: _RANK[b], default=GAP)
    reason = ""
    if f.outcome in FAILURES:
        worst, reason = RED, f.status_text
    elif worst in (AMBER, RED):
        reason = "; ".join(f"{s.name.lower()} {s.value}" for s in measured if s.band == worst)
    return OnTrack(worst, _WORDS[worst], tuple(signals), reason)


def _stall(f: Feature, norms: Norms, now: datetime) -> Signal:
    name, norm = "No activity while running", norms.stall_minutes.describe()
    if not f.in_progress:
        return Signal(name, "—", GAP, norm, "finished")
    if f.open_wait() is not None:
        return Signal(name, "—", GAP, norm, "waiting for a person, not running")
    if f.builds:
        return Signal(name, "—", GAP, norm,
                      "not measured during a build: Forge records nothing between a build's start and finish, "
                      "so build time is the signal for a long build")
    last = f.last_activity_at()
    if last is None:
        return Signal(name, "—", GAP, norm, "no activity recorded")
    quiet = max(0.0, (now - last).total_seconds())
    return Signal(name, _mins(quiet), norms.stall_minutes.band(quiet / 60), norm, "since the last planning step")


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
