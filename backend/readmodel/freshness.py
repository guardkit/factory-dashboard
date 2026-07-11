"""Freshness + the F-5 layering (ux §5.6) — the honesty arithmetic, in one testable place.

Three signals, never conflated (design §1 F-5, restored verbatim by ux §5.6):

1. **Projector liveness** = the heartbeat (`service_health('projector')`, upserted every 10 s
   regardless of event flow). Stale (>60 s) ⇒ the WHOLE projection is suspect: page-level LAGGING
   banner, panels dim, and ALL stalled/dwell verdicts are suppressed.
2. **Per-stream recency** = `consumer_watermarks.last_event_at`. A stalled/dwell verdict is
   manufactured ONLY when the dwell threshold is breached WHILE the feeding stream's watermark
   stayed fresh over that same window (other events flowed → the silence is real). If the
   watermark is stale across the verdict window, the panel reports "projection lagging (since ⟨ts⟩)"
   and NO stalled issue is manufactured — quiet-or-wedged cannot be distinguished from here.
3. **Quiet with no verdict at stake** — a stream with no in-flight work and no recent events is
   neutral ("no events since ⟨ts⟩"): nothing was suppressed because no verdict needed it.

Nothing here touches a DB or a template; it is pure functions over timestamps, so the three F-5
acceptance cases (ux §10.6) are unit-testable in isolation.
"""

from __future__ import annotations

from datetime import datetime

from backend.readmodel.viewmodels import (
    BUS_FRESH_SECS,
    PROJECTOR_STALE_SECS,
    PanelState,
)

# Dwell threshold that would produce a "stalled" issue (design §1 norms: stage dwell >120 min).
STALLED_DWELL_SECS = 120 * 60


def age_secs(ts: datetime | None, now: datetime) -> float | None:
    """Age of a timestamp in seconds, or None when the timestamp is absent."""
    if ts is None:
        return None
    return (now - ts).total_seconds()


def projector_alive(heartbeat_at: datetime | None, now: datetime, *, stale_secs: int = PROJECTOR_STALE_SECS) -> bool:
    """Signal 1: the projector heartbeat is fresh. A missing heartbeat is NOT alive (honest dark)."""
    age = age_secs(heartbeat_at, now)
    return age is not None and age < stale_secs


def watermark_fresh(last_event_at: datetime | None, now: datetime, *, threshold_secs: int = BUS_FRESH_SECS) -> bool:
    """Signal 2: the feeding stream's watermark is fresh (events flowed recently)."""
    age = age_secs(last_event_at, now)
    return age is not None and age < threshold_secs


def should_manufacture_stalled(
    *,
    dwell_secs: float,
    projector_is_alive: bool,
    stream_watermark_fresh: bool,
    dwell_threshold_secs: int = STALLED_DWELL_SECS,
) -> bool:
    """F-5 verbatim. Manufacture a stalled/dwell verdict ONLY when:
    the projector is live (else the whole projection is suspect — suppress) AND the dwell
    threshold is breached AND the feeding stream's watermark stayed fresh over the window
    (else quiet-or-wedged is indistinguishable — report "projection lagging", suppress the issue).
    """
    if not projector_is_alive:
        return False
    if dwell_secs < dwell_threshold_secs:
        return False
    return stream_watermark_fresh


def panel_state(
    *,
    projector_is_alive: bool,
    last_event_at: datetime | None,
    now: datetime,
    row_count: int,
    threshold_secs: int = BUS_FRESH_SECS,
) -> PanelState:
    """Resolve a bus-fed panel's §5.2 state.

    - projector stale ⇒ LAGGING (page-level suspicion dims every panel);
    - watermark stale across the window ⇒ LAGGING (panel-level, F-5);
    - fresh evidence + zero rows ⇒ TRUE-EMPTY (the pinned empty copy is safe to assert);
    - fresh evidence + rows ⇒ LIVE.
    """
    if not projector_is_alive:
        return PanelState.LAGGING
    if not watermark_fresh(last_event_at, now, threshold_secs=threshold_secs):
        return PanelState.LAGGING
    return PanelState.TRUE_EMPTY if row_count == 0 else PanelState.LIVE


def mirror_panel_state(
    *,
    projector_is_alive: bool,
    mirrored_at: datetime | None,
    now: datetime,
    row_count: int,
    fresh_secs: int,
) -> PanelState:
    """A mirror-fed panel (P5) resolves TRUE-EMPTY only off a FRESH mirror pass; a stale or
    never-run mirror renders LAGGING, never TRUE-EMPTY (ux §4.1) — so an un-updated mirror can
    never read as "genuinely no runs"."""
    if not projector_is_alive:
        return PanelState.LAGGING
    age = age_secs(mirrored_at, now)
    if age is None or age >= fresh_secs:
        return PanelState.LAGGING
    return PanelState.TRUE_EMPTY if row_count == 0 else PanelState.LIVE
