"""S2 F-5 layering (ux §5.6 / §10.6) — the three verbatim cases + panel-state resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.readmodel import freshness as fr
from backend.readmodel.viewmodels import PanelState

NOW = datetime(2026, 7, 11, 14, 13, 34, tzinfo=UTC)


def _ago(secs: float) -> datetime:
    return NOW - timedelta(seconds=secs)


# --- projector liveness (signal 1) -------------------------------------------


def test_projector_alive_only_when_heartbeat_fresh() -> None:
    assert fr.projector_alive(_ago(5), NOW) is True
    assert fr.projector_alive(_ago(120), NOW) is False   # >60s stale
    assert fr.projector_alive(None, NOW) is False         # missing heartbeat is NOT alive (honest dark)


# --- F-5: the three cases (ux §10.6) -----------------------------------------


def test_heartbeat_stale_suppresses_all_stalled_verdicts() -> None:
    """Case 1: projector stale ⇒ the whole projection is suspect ⇒ NO stalled issue manufactured,
    even with a long dwell and a (last-known) fresh watermark."""
    assert fr.should_manufacture_stalled(
        dwell_secs=3 * 3600, projector_is_alive=False, stream_watermark_fresh=True
    ) is False


def test_dwell_breach_with_stale_watermark_reports_lagging_not_stalled() -> None:
    """Case 2: dwell breached but the feeding stream's watermark went stale over the window ⇒
    quiet-or-wedged is indistinguishable ⇒ report lagging, manufacture NO stalled issue (F-5)."""
    assert fr.should_manufacture_stalled(
        dwell_secs=3 * 3600, projector_is_alive=True, stream_watermark_fresh=False
    ) is False


def test_dwell_breach_with_fresh_watermark_manufactures_stalled() -> None:
    """Case 3: dwell breached WHILE the stream stayed fresh (other events flowed) ⇒ the silence is
    real ⇒ manufacture the stalled issue."""
    assert fr.should_manufacture_stalled(
        dwell_secs=3 * 3600, projector_is_alive=True, stream_watermark_fresh=True
    ) is True


def test_quiet_stream_no_dwell_no_verdict() -> None:
    """A quiet stream with no verdict at stake: nothing suppressed, nothing manufactured."""
    assert fr.should_manufacture_stalled(
        dwell_secs=60, projector_is_alive=True, stream_watermark_fresh=True
    ) is False


# --- panel-state resolution --------------------------------------------------


def test_panel_state_live_true_empty_and_lagging() -> None:
    assert fr.panel_state(projector_is_alive=True, last_event_at=_ago(3), now=NOW, row_count=2) is PanelState.LIVE
    assert fr.panel_state(projector_is_alive=True, last_event_at=_ago(3), now=NOW, row_count=0) is PanelState.TRUE_EMPTY
    # projector stale ⇒ LAGGING regardless of watermark
    assert fr.panel_state(projector_is_alive=False, last_event_at=_ago(3), now=NOW, row_count=2) is PanelState.LAGGING
    # stale watermark ⇒ LAGGING (panel-level, F-5)
    assert fr.panel_state(projector_is_alive=True, last_event_at=_ago(90), now=NOW, row_count=2) is PanelState.LAGGING


def test_mirror_panel_state_true_empty_only_off_fresh_pass() -> None:
    fresh = 60
    assert fr.mirror_panel_state(projector_is_alive=True, mirrored_at=_ago(10), now=NOW,
                                 row_count=0, fresh_secs=fresh) is PanelState.TRUE_EMPTY
    # a stale/never-run mirror renders LAGGING, never TRUE-EMPTY (ux §4.1)
    assert fr.mirror_panel_state(projector_is_alive=True, mirrored_at=_ago(999), now=NOW,
                                 row_count=0, fresh_secs=fresh) is PanelState.LAGGING
    assert fr.mirror_panel_state(projector_is_alive=True, mirrored_at=None, now=NOW,
                                 row_count=0, fresh_secs=fresh) is PanelState.LAGGING
