"""The chat's queries (design 2026-10-17, requirement 9): typed, read-only, over the pages' own read layer.

Each query reads through `backend/readmodel/dbread.py`, the functions the pages render, so a page
and an answer cannot disagree. Each returns a `Result`: plain fields (every figure already worked
out and formatted as the page shows it) and references to the records (feature, project, issue,
period) as links to the pages. The answer text is written from these fields by a fixed template
(`backend/chat/answers.py`); the model never writes a figure, a status or a name.

A name that matches nothing says so; a name that matches more than one record lists them all and
asks which (status ``ambiguous``), never a guess.

Cost and time are not measured yet (they arrive with part B). `cost_summary` and `time_summary` are
registered so a question about them gets a plain "not measured yet", never a made-up number.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from backend import db
from backend.readmodel import dbread
from backend.readmodel import viewmodels as vm
from backend.readmodel.factory import STATIONS, Factory, Feature, load_factory

# --- what a query returns ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """A record the answer rests on, as a link to the page that shows it."""

    kind: str    # 'feature' | 'project' | 'issue' | 'period' | 'page'
    label: str
    href: str


#: found: the answer; not_found: nothing matched; ambiguous: more than one record matched;
#: not_measured: the dashboard does not measure this yet; bad_period: the dates could not be read.
STATUSES = ("found", "not_found", "ambiguous", "not_measured", "bad_period")


@dataclass(frozen=True)
class Result:
    query: str
    status: str
    fields: dict[str, object]
    refs: tuple[Ref, ...] = ()
    candidates: tuple[Ref, ...] = field(default=())


@dataclass(frozen=True)
class Context:
    """What every query needs: the dashboard store and the moment the question was asked."""

    db_path: Path
    now: datetime


# --- the main pages, for a refusal or a "not measured" answer ----------------------------------------

PAGES: dict[str, Ref] = {
    "features": Ref("page", "Features", "/features"),
    "delivered": Ref("page", "Delivered", "/delivered"),
    "issues": Ref("page", "Issues", "/issues"),
    "reports": Ref("page", "Weekly report", "/reports"),
}


# --- finding a feature or a project by name ----------------------------------------------------------

_CODE_RE = re.compile(r"^feat-[0-9a-z]+$", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "of", "for", "to", "and", "that", "feature", "called", "named", "one",
                   "with", "in", "on", "by", "is", "it", "its", "which", "this", "about", "project"})
MAX_LISTED = 5


def _norm(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def _feature_ref(f: Feature) -> Ref:
    return Ref("feature", f.label, f.link)


def _project_ref(project: str) -> Ref:
    return Ref("project", project, dbread.project_link(project))


def find_features(factory: Factory, name: str) -> list[Feature]:  # noqa: PLR0911 (one return per rule)
    """Features matching a code, a request id or words of the title, best matches first.

    An exact code or id gives that feature. Otherwise: an exact title; then titles or request
    sentences containing the name; then those containing every word of it. Several features can
    share a title (the same sentence sent twice): all of them are returned, so the caller can ask.
    """
    name = (name or "").strip()
    if not name:
        return []
    if _CODE_RE.match(name):
        found = factory.feature(name.upper())
        return [found] if found else []
    exact = factory.feature(name)
    if exact is not None:
        return [exact]
    wanted = _norm(name)
    if not wanted:
        return []
    by_title = [f for f in factory.features if _norm(f.title) == wanted]
    if by_title:
        return by_title
    inside = [f for f in factory.features if wanted in _norm(f.title) or wanted in _norm(f.request_text)]
    if inside:
        return inside
    words = _words(name)
    if not words:
        return []
    return [f for f in factory.features if words <= _words(f.title) | _words(f.request_text)]


def projects_on_record(factory: Factory) -> list[str]:
    seen: list[str] = []
    for f in factory.features:
        if f.project and f.project not in seen:
            seen.append(f.project)
    return seen


def find_projects(factory: Factory, name: str) -> list[str]:
    """Projects (`owner/name`) matching a name: exactly, by the part after the slash, or containing it."""
    wanted = _norm(name)
    if not wanted:
        return []
    known = projects_on_record(factory)
    exact = [p for p in known if _norm(p) == wanted]
    if exact:
        return exact
    by_name = [p for p in known if _norm(p.rsplit("/", 1)[-1]) == wanted]
    if by_name:
        return by_name
    return [p for p in known if wanted in _norm(p)]


# --- periods ------------------------------------------------------------------------------------------

_ISO = r"(\d{4}-\d{2}-\d{2})"
_RANGE_RE = re.compile(rf"^{_ISO}\s*(?:to|until|\.\.|-|–)\s*{_ISO}$")
_WEEK_OF_RE = re.compile(rf"^week (?:of|starting|beginning) {_ISO}$")
_DAY_RE = re.compile(rf"^(?:on )?{_ISO}$")


@dataclass(frozen=True)
class Period:
    start: date
    end: date      # inclusive
    name: str      # "this week", "in the last 7 days"; "" for dates given outright

    @property
    def window(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()

    @property
    def label(self) -> str:
        """Reads after a verb: "this week (12 Oct 2026 to 18 Oct 2026)", "between 1 Oct 2026 and …"."""
        if self.name:
            span = f"{_day(self.start)} to {_day(self.end)}" if self.start != self.end else _day(self.start)
            return f"{self.name} ({span})"
        if self.start == self.end:
            return f"on {_day(self.start)}"
        return f"between {_day(self.start)} and {_day(self.end)}"


def _day(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def resolve_period(text: str | None, now: datetime) -> Period | None:
    """A few plain phrases or explicit dates, read on the London calendar. Weeks run Monday to
    Sunday, as on the Delivered page. None when the phrase is not one of these."""
    phrase = " ".join((text or "").lower().split()).strip(" .")
    today = vm.to_london(now).date()
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    named: dict[str, tuple[date, date]] = {
        "": (monday, monday + timedelta(days=6)),
        "this week": (monday, monday + timedelta(days=6)),
        "last week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "last 7 days": (today - timedelta(days=6), today),
        "last 30 days": (today - timedelta(days=29), today),
        "this month": (first, today),
        "last month": (last_month_end.replace(day=1), last_month_end),
    }
    if phrase in named:
        lo, hi = named[phrase]
        words = {"": "this week", "last 7 days": "in the last seven days", "last 30 days": "in the last thirty days"}
        return Period(lo, hi, words.get(phrase, phrase))
    try:
        if m := _RANGE_RE.match(phrase):
            lo, hi = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
            return Period(lo, hi, "") if lo <= hi else None
        if m := _WEEK_OF_RE.match(phrase):
            d = date.fromisoformat(m.group(1))
            start = d - timedelta(days=d.weekday())
            return Period(start, start + timedelta(days=6), "in the week of Monday " + _day(start))
        if m := _DAY_RE.match(phrase):
            d = date.fromisoformat(m.group(1))
            return Period(d, d, "")
    except ValueError:
        return None
    return None


PERIOD_PHRASES = ("this week", "last week", "today", "yesterday", "last 7 days", "last 30 days",
                  "this month", "last month", "week of YYYY-MM-DD", "YYYY-MM-DD to YYYY-MM-DD")


def _period_ref(p: Period, page: str) -> Ref:
    return Ref("period", f"{page.title()} page, {p.label}", f"/{page}?" + urlencode({"from": p.window[0],
                                                                                    "to": p.window[1]}))


def _bad_period(query: str, text: str | None) -> Result:
    return Result(query, "bad_period", {"period_text": text or ""}, refs=())


# --- shared helpers -------------------------------------------------------------------------------------


def _with_store(ctx: Context, read: Callable[[sqlite3.Connection, Factory], Result]) -> Result:
    conn = db.connect_ro(ctx.db_path)
    try:
        return read(conn, load_factory(conn, ctx.now))
    finally:
        conn.close()


def _stale_note(ctx: Context) -> str:
    """The page's own stale-data banner, word for word, or "" when the record is fresh."""
    chrome = dbread.chrome_view(ctx.db_path, ctx.now)
    return chrome.banner.text if chrome.banner else ""


def _ambiguous(query: str, kind: str, name: str, refs: Iterable[Ref]) -> Result:
    refs = tuple(refs)
    shown = refs[:MAX_LISTED]
    return Result(query, "ambiguous", {"kind": kind, "name": name, "count": str(len(refs)),
                                       "listed": [r.label for r in shown], "more": str(len(refs) - len(shown))},
                  candidates=shown)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _station(f: Feature) -> str:
    return STATIONS[min(f.station, len(STATIONS) - 1)]


def _resolve_project(query: str, factory: Factory, name: str | None) -> tuple[str | None, Result | None]:
    """(project, None) when found or not asked for; (None, a not-found or ambiguous result) otherwise."""
    if not name:
        return None, None
    found = find_projects(factory, name)
    if not found:
        return None, Result(query, "not_found", {"kind": "project", "name": name})
    if len(found) > 1:
        return None, _ambiguous(query, "project", name, (_project_ref(p) for p in found))
    return found[0], None


# --- the queries ---------------------------------------------------------------------------------------


def feature_status(ctx: Context, feature: str) -> Result:
    """Where one feature stands, as its page shows it."""

    def read(_conn: sqlite3.Connection, factory: Factory) -> Result:
        found = find_features(factory, feature)
        if not found:
            return Result("feature_status", "not_found", {"kind": "feature", "name": feature})
        if len(found) > 1:
            return _ambiguous("feature_status", "feature", feature,
                              (Ref("feature", f"{f.label}, sent {_sent(f, ctx.now)}", f.link) for f in found))
        f = found[0]
        page = dbread.feature_page(ctx.db_path, f.key, ctx.now)
        assert page is not None
        _chrome, fp = page
        open_issues = [i for i in fp.issues if not i.closed_label]
        fields: dict[str, object] = {
            "label": f.label, "project": fp.project, "status_text": fp.status_text, "station": _station(f),
            # Finished without being delivered: the journey marks this step "stopped", not reached.
            "stopped": "yes" if not f.in_progress and not f.delivered_at else "",
            "verdict": fp.verdict.label, "verdict_reason": fp.verdict_reason, "elapsed": fp.elapsed,
            "sent": _when(fp.sent_label), "attempts": str(len(fp.attempts)),
            "delivered_on": dbread.delivered_row(f).delivered_date if f.delivered_at else "",
            "mission_line": fp.mission_line,
            "open_issues": [f"{i.kind}: {i.detail}" if i.detail else i.kind for i in open_issues],
            "cost": fp.spend.text, "stale_note": _stale_note(ctx),
        }
        refs = [_feature_ref(f)]
        if fp.project:
            refs.append(_project_ref(fp.project))
        return Result("feature_status", "found", fields, tuple(refs))

    return _with_store(ctx, read)


def _sent(f: Feature, now: datetime) -> str:
    return _when(vm.fmt_wall(f.sent_at, now=now)) if f.sent_at else "at a time not recorded"


_CLOCK_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _when(wall_label: str) -> str:
    """A page's time label ("08:14:05" for today, "17 Oct 08:14" otherwise) as words after "sent"."""
    if wall_label in ("", "—"):
        return "at a time not recorded"
    return f"today at {wall_label}" if _CLOCK_RE.match(wall_label) else f"on {wall_label}"


def project_summary(ctx: Context, project: str, period: str | None = None) -> Result:
    """A project's headline, as its page shows it; with a period, also what was delivered in it."""
    p = resolve_period(period, ctx.now) if period else None
    if period and p is None:
        return _bad_period("project_summary", period)

    def read(conn: sqlite3.Connection, factory: Factory) -> Result:
        name, problem = _resolve_project("project_summary", factory, project)
        if problem is not None or name is None:
            return problem or Result("project_summary", "not_found", {"kind": "project", "name": project})
        view = dbread.project_view(ctx.db_path, name, ctx.now)
        fields: dict[str, object] = {
            "project": name, "in_progress": str(view.in_progress), "delivered_7d": str(view.delivered_7d),
            "delivered_total": str(view.delivered_total), "open_issue_count": str(len(view.issues)),
            "open_issues": [f"{i.kind}: {i.subject}" for i in view.issues[:MAX_LISTED]],
            "more_issues": str(max(0, len(view.issues) - MAX_LISTED)), "cost": view.spend.text,
            "period_label": "", "period_delivered": "", "stale_note": _stale_note(ctx),
        }
        refs = [_project_ref(name)]
        if p is not None:
            lo, hi = dbread._window_bounds(*p.window)
            fields["period_label"] = p.label
            fields["period_delivered"] = str(len(dbread.delivered_in(factory, lo, hi, (name,))))
            refs.append(_period_ref(p, "delivered"))
        return Result("project_summary", "found", fields, tuple(refs))

    return _with_store(ctx, read)


def open_issues(ctx: Context, project: str | None = None) -> Result:
    """The open issues, as the Issues page lists them, optionally for one project."""

    def read(_conn: sqlite3.Connection, factory: Factory) -> Result:
        name, problem = _resolve_project("open_issues", factory, project)
        if problem is not None:
            return problem
        view = dbread.issues_view(ctx.db_path, ctx.now)
        rows = [r for r in view.open_rows if name is None or r.project == name]
        shown = rows[:MAX_LISTED]
        fields: dict[str, object] = {
            "project": name or "", "count": str(len(rows)), "more": str(len(rows) - len(shown)),
            "rows": [{"kind": r.kind, "subject": r.subject, "detail": r.detail, "age": r.age,
                      "how_serious": r.band.label} for r in shown],
            "stale_note": _stale_note(ctx),
        }
        refs = [Ref("issue", f"{r.kind}: {r.subject}" if r.subject else r.kind, r.link) for r in shown]
        refs.append(_project_ref(name) if name else PAGES["issues"])
        return Result("open_issues", "found", fields, tuple(refs))

    return _with_store(ctx, read)


def delivered(ctx: Context, period: str | None = None, project: str | None = None) -> Result:
    """What was merged and checked in a period (this week by default), as the Delivered page shows it."""
    p = resolve_period(period, ctx.now)
    if p is None:
        return _bad_period("delivered", period)

    def read(_conn: sqlite3.Connection, factory: Factory) -> Result:
        name, problem = _resolve_project("delivered", factory, project)
        if problem is not None:
            return problem
        panel = dbread.delivered_view(ctx.db_path, p.window, ctx.now).panel
        rows = [r for r in panel.rows if name is None or r.project == name]
        shown = rows[:MAX_LISTED * 2]
        fields: dict[str, object] = {
            "project": name or "", "period_label": p.label, "count": str(len(rows)),
            "more": str(len(rows) - len(shown)),
            "rows": [{"label": f"{r.title} ({r.code})" if r.code else r.title, "on": r.delivered_date}
                     for r in shown],
            # Factory-wide, so only when no project was asked for.
            "other_endings": "" if name else panel.other_endings,
            "stale_note": _stale_note(ctx),
        }
        refs = [Ref("feature", f"{r.title} ({r.code})" if r.code else r.title, r.link) for r in shown]
        refs.append(_period_ref(p, "delivered"))
        if name:
            refs.append(_project_ref(name))
        return Result("delivered", "found", fields, tuple(refs))

    return _with_store(ctx, read)


def weekly_report(ctx: Context, period: str | None = None, project: str | None = None) -> Result:
    """The weekly report's headline for a period (this week by default), as the Reports page shows it.

    For one project, the delivered and in-progress counts are that project's rows of the same
    report; the plan and issue figures on the report cover every project, so they are left out."""
    p = resolve_period(period, ctx.now)
    if p is None:
        return _bad_period("weekly_report", period)

    def read(_conn: sqlite3.Connection, factory: Factory) -> Result:
        name, problem = _resolve_project("weekly_report", factory, project)
        if problem is not None:
            return problem
        report = dbread.weekly_report_view(ctx.db_path, "operator", p.window, ctx.now)
        h = report.headline
        delivered_rows = [r for r in report.delivered_rows if name is None or r.project == name]
        in_flight_rows = [r for r in report.in_flight_rows if name is None or r.project == name]
        milestones = h.behind_count + h.on_target_count + h.ahead_count
        fields: dict[str, object] = {
            "project": name or "", "period_label": p.label,
            "withheld": "yes" if h.withheld else "", "withheld_since": h.withheld_since,
            "delivered_count": str(len(delivered_rows)), "in_flight_count": str(len(in_flight_rows)),
            "delivered": [f"{r.title} ({r.code})" if r.code else r.title for r in delivered_rows[:MAX_LISTED]],
            "more_delivered": str(max(0, len(delivered_rows) - MAX_LISTED)),
            "plan_agreed": "yes" if milestones else "",
            "behind": str(h.behind_count), "on_target": str(h.on_target_count), "ahead": str(h.ahead_count),
            "issues_opened": str(report.issues.opened), "issues_closed": str(report.issues.closed),
            "oldest_open": report.issues.oldest_open_age, "cost": h.spend.text,
        }
        refs = [Ref("period", f"Weekly report, {p.label}",
                    "/reports?" + urlencode({"from": p.window[0], "to": p.window[1]}))]
        if not h.withheld:
            refs += [Ref("feature", f"{r.title} ({r.code})" if r.code else r.title, r.link)
                     for r in delivered_rows[:MAX_LISTED]]
        if name:
            refs.append(_project_ref(name))
        return Result("weekly_report", "found", fields, tuple(refs))

    return _with_store(ctx, read)


def cost_summary(ctx: Context, project: str | None = None, feature: str | None = None,
                 period: str | None = None) -> Result:
    """Cost is not measured yet (part B). Never a number until it is."""
    return Result("cost_summary", "not_measured", {"what": "cost"}, (PAGES["reports"],))


def time_summary(ctx: Context, project: str | None = None, feature: str | None = None,
                 period: str | None = None) -> Result:
    """Time split into machine time and waiting is not measured yet (part B). Never a number until it is."""
    return Result("time_summary", "not_measured", {"what": "time"}, (PAGES["features"],))
