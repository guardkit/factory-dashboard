"""The FACTORY_DASH_DB env contract on the APP side (the compose front door's one requirement).

The projector has honoured ``FACTORY_DASH_DB`` since S1; the composed container mounts the
read-model at ``/data`` and points BOTH processes at it through that one variable, so the app must
honour it too. An explicit ``db_path`` argument still wins (the test-suite idiom), and with neither
the repo-root default stands — bare/dev runs are byte-identical to before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from backend.app import BASE_DIR, create_app

from .conftest import TENANTS_TEST


def test_env_db_path_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_db = tmp_path / "mounted" / "readmodel.db"
    env_db.parent.mkdir()
    monkeypatch.setenv("FACTORY_DASH_DB", str(env_db))
    app = create_app(tenants_path=TENANTS_TEST)
    assert app.state.db_path == env_db


def test_explicit_db_path_beats_the_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_DASH_DB", str(tmp_path / "env.db"))
    explicit = tmp_path / "explicit.db"
    app = create_app(db_path=explicit, tenants_path=TENANTS_TEST)
    assert app.state.db_path == explicit


def test_bare_run_keeps_the_repo_root_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FACTORY_DASH_DB", raising=False)
    monkeypatch.setattr("backend.db.init_db", lambda *a, **k: None)
    app = create_app(tenants_path=TENANTS_TEST)
    assert app.state.db_path == BASE_DIR / "readmodel.db"
