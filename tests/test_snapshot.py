"""Each page and fragment is built from one read snapshot (Codex review, round 4, R2).

A feed pass can commit while a page is being built. If the page's content, read time and data
version came from different moments, old content could carry the new version: later polls would
then see a matching version and never fetch the content again. Here a pass commits in the middle
of each view (after the features are read, before the read time and version are), every time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend.projector.forge_mirror import mirror_once
from backend.projector.store import open_rw
from backend.readmodel import dbread
from backend.readmodel.norms import Norms

from tests import forge_fake
from tests.forge_fake import ForgeFake, delivered_feature, store_from

NOW = datetime.now(UTC).replace(microsecond=0)


def _chrome_and_read_time(view: object) -> tuple[str, float | None]:
    # feature_page returns (chrome, page); the others carry .chrome; a fragment only its own time.
    chrome = view[0] if isinstance(view, tuple) else getattr(view, "chrome", None)
    if chrome is not None:
        return chrome.version, chrome.read_as_of.asof_epoch
    return "", view.as_of.asof_epoch     # type: ignore[attr-defined]  # a live-update fragment


VIEWS: dict[str, Callable[[Path], object]] = {
    "Now": lambda s: dbread.home_view(s, NOW),
    "Features": lambda s: dbread.features_view(s, 7, NOW),
    "Feature": lambda s: dbread.feature_page(s, "FEAT-0003", NOW),
    "Project": lambda s: dbread.project_view(s, forge_fake.REPO, NOW),
    "Projects": lambda s: dbread.projects_view(s, NOW),
    "Issues": lambda s: dbread.issues_view(s, NOW),
    "Weekly report": lambda s: dbread.weekly_report_view(s, "operator", now=NOW),
    "Live-update fragment": lambda s: dbread.panel_view(s, "p2", NOW),
}


@pytest.mark.parametrize("name", list(VIEWS))
def test_a_feed_pass_in_the_middle_of_a_page_never_mixes_old_content_with_the_new_version(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forge_fake, "T0", NOW - timedelta(hours=3))
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)
    fake.planning("c3", "Count the notes.")
    fake.build("b3", "FEAT-0003", "c3", status="RUNNING", queued=100, started=100, completed=None)
    store = tmp_path / "readmodel.db"
    store_from(fake, store, now=NOW).close()
    before = dbread.chrome_view(store, NOW)

    # Forge records the build finishing; the feed copies it in the middle of the view: after the
    # features were read (load_factory), before the read time and version are (page_chrome).
    fake.conn.execute("UPDATE builds SET status='COMPLETE', completed_at=? WHERE build_id='b3'",
                      (forge_fake.at(150),))
    fake.merge_offer("b3", 151)
    real_load_norms = dbread.load_norms
    passes: list[bool] = []

    def load_norms_with_a_pass_committing_now() -> Norms:
        if not passes:
            writer = open_rw(store)
            try:
                passes.append(mirror_once(writer, fake.path, NOW + timedelta(seconds=5)))
            finally:
                writer.close()
        return real_load_norms()

    monkeypatch.setattr(dbread, "load_norms", load_norms_with_a_pass_committing_now)
    view = VIEWS[name](store)
    assert passes == [True]                                   # the pass did commit a change mid-view

    after = dbread.chrome_view(store, NOW)
    assert after.version != before.version and after.read_as_of.asof_epoch != before.read_as_of.asof_epoch

    # The view is all from before the pass: its version and read time match its (old) content.
    version, read_at = _chrome_and_read_time(view)
    if version:
        assert version == before.version, name
    assert read_at == before.read_as_of.asof_epoch, name
    if name == "Features":
        row = next(r for r in view.features.rows if r.title == "Count the notes.")  # type: ignore[attr-defined]
        assert row.status_text == "Building"
