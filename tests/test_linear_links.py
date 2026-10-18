"""Each project's Linear link (design "Linear front door", 11.3): read from the Linear door's file,
only linear.app addresses kept, shown on the project page head and the Projects page row, and
nothing at all rendered without one."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from backend.app import create_app
from backend.readmodel.linear import LINEAR_FILE, linear_links
from starlette.testclient import TestClient

from tests import forge_fake
from tests.conftest import LOOPBACK_URL, login
from tests.forge_fake import REPO, ForgeFake, delivered_feature, store_from

TENANTS_TEST = Path(__file__).resolve().parent / "fixtures" / "tenants_test.yaml"
URL = "https" + "://linear.app/acme-team/project/notes-0a1b2c3d4e5f"   # a made-up project


def _write(folder: Path, projects: object, *, raw: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps({"written_at": "2026-10-18T09:00:00+00:00", "projects": projects})
    (folder / LINEAR_FILE).write_text(text, encoding="utf-8")


@pytest.fixture
def pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(forge_fake, "T0", datetime.now(UTC) - timedelta(hours=3))
    fake = ForgeFake(tmp_path / "forge.db")
    delivered_feature(fake, 1)
    store = tmp_path / "readmodel.db"
    store_from(fake, store).close()
    with TestClient(create_app(db_path=store, tenants_path=TENANTS_TEST), base_url=LOOPBACK_URL) as client:
        login(client)
        yield client


def _head(body: str) -> str:
    start = body.index('<header class="page-head')
    return body[start:body.index("</header>", start)]


def _row(body: str) -> str:
    row = body[body.index('<tr class="project-row">'):]
    return row[:row.index("</tr>")]


def test_the_link_shows_on_the_project_page_and_its_row(pages: TestClient, no_linear_door: Path) -> None:
    _write(no_linear_door, {REPO: {"name": "notes", "url": URL}, "acme/other": {"name": "other", "url": URL + "x"}})
    head = _head(pages.get(f"/projects/{REPO}").text)
    assert f'href="{URL}"' in head and "#i-external" in head and "Linear project" in head
    assert 'class="page-head__meta"' in head
    row = _row(pages.get("/projects").text)
    assert f'href="{URL}"' in row and "#i-external" in row and f'aria-label="Linear project for {REPO}"' in row


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"projects": []}', '{"projects": {"acme/notes": "x"}}'])
def test_without_a_usable_file_nothing_is_rendered(pages: TestClient, no_linear_door: Path,
                                                    content: str | None) -> None:
    if content is not None:
        _write(no_linear_door, None, raw=content)
    for path in (f"/projects/{REPO}", "/projects"):
        body = pages.get(path).text
        assert "linear.app" not in body and "Linear" not in body and "#i-external" not in body
    assert 'class="page-head__meta"' not in _head(pages.get(f"/projects/{REPO}").text)


def test_a_project_with_no_entry_shows_no_link(pages: TestClient, no_linear_door: Path) -> None:
    _write(no_linear_door, {"acme/other": {"name": "other", "url": URL}})
    assert "Linear" not in pages.get(f"/projects/{REPO}").text
    assert "Linear" not in _row(pages.get("/projects").text)


@pytest.mark.parametrize("url", [
    "https" + "://evil.example/linear.app/",
    "https" + "://linear.app.evil.example/x",
    "http" + "://linear.app/acme/project/x",
    "javascript:alert(1)//https" + "://linear.app/",
    "https" + '://linear.app/x" onmouseover="alert(1)',
    "https" + "://linear.app/x y",
    "https" + "://linear.app/x\n",
    42,
    None,
])
def test_only_linear_addresses_are_kept(pages: TestClient, no_linear_door: Path, url: object) -> None:
    _write(no_linear_door, {REPO: {"name": "notes", "url": url}})
    assert linear_links(no_linear_door) == {}
    assert "Linear" not in pages.get(f"/projects/{REPO}").text


def test_the_reader_keeps_good_entries_and_drops_bad_ones(tmp_path: Path) -> None:
    _write(tmp_path, {REPO: {"name": "notes", "url": URL}, "acme/bad": {"url": "https" + "://example.com/"},
                      "no-slash": {"url": URL}, "acme/nameless": {"url": URL}})
    assert linear_links(tmp_path) == {REPO: URL, "acme/nameless": URL}
    assert linear_links(tmp_path / "missing") == {}
