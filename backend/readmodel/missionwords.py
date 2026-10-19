"""A mission's status and its reason, in plain words (missions design, section 11, "The dashboard").

- A mission with a stop asked for that has not closed yet is "stopping" wherever its status shows.
- A closed mission's reason is Forge's `closed_reason`, made readable. Forge writes most reasons as
  plain sentences already ("nothing landed", "nobody answered the mission plan card in time"); two
  carry an approver identity, which is never shown as it is:
  - a stop: "stopped by <who> at <ISO time>; <n> landed on the branch, <m> did not" (or "... before
    it started") (`lifecycle/missions.py`, `close_if_drained` and `request_stop`);
  - a declined merge card: "<who> declined the merge card" (`pipeline/mission_finish.py`).
  The person is named only by the display name the dashboard's accounts give that identity;
  otherwise the sentence leaves them out ("stopped at 14:20:05"). Any other approver identity
  still found in a reason becomes "someone". The time is shown as the pages show times.
- A reason that is only a code ("merged-deploy-failed") is shown with spaces for its hyphens.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from backend.readmodel import viewmodels as vm
from backend.readmodel.factory import Mission, parse_dt

_STATUS_WORDS = {
    "PROPOSED": "proposed", "REJECTED": "rejected", "NOT_STARTED": "not started", "STARTING": "starting",
    "RUNNING": "running", "READY": "ready for its merge", "MERGING": "merging", "MERGED": "merged",
    "NOT_MERGED": "not merged",
}

_STOPPED = re.compile(r"stopped by (?P<who>.+?) at (?P<at>\S+?)(?P<rest>(?:;| before).*)?")
_DECLINED = re.compile(r"(?P<who>\S+) declined the merge card(?P<rest>.*)")
_CODE = re.compile(r"[a-z]+(?:[-_][a-z]+)+")
#: An approver identity as Forge records one: a chat member id (U… or W…, upper case with a digit)
#: or a dashboard approver ("dashboard:<name>").
_IDENTITY = re.compile(r"\b[UW](?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b|\bdashboard:[^\s;,.]+")

Names = Mapping[str, str]


def status_words(m: Mission) -> str:
    """The mission's status in lower-case words; "stopping" while a stop drains."""
    if m.stopping:
        return "stopping"
    return _STATUS_WORDS.get(m.status, m.status.replace("_", " ").lower())


def _name(identity: str, names: Names | None) -> str:
    return (names or {}).get(identity.strip(), "").strip()


def _when(raw: str, now: datetime) -> str:
    dt = parse_dt(raw)
    return vm.fmt_wall(dt, now=now) if dt else ""


def _scrub(text: str, names: Names | None) -> str:
    return _IDENTITY.sub(lambda mt: _name(mt.group(0), names) or "someone", text)


def reason_words(reason: str, now: datetime, names: Names | None = None) -> str:
    """Forge's `closed_reason`, readable, with no approver identity in it and no full stop."""
    text = " ".join((reason or "").split()).rstrip(".")
    if not text:
        return ""
    stopped = _STOPPED.fullmatch(text)
    if stopped:
        who, when = _name(stopped["who"], names), _when(stopped["at"], now)
        head = "stopped" + (f" by {who}" if who else "") + (f" at {when}" if when else "")
        return _scrub(head + (stopped["rest"] or ""), names)
    declined = _DECLINED.fullmatch(text)
    if declined:
        who = _name(declined["who"], names)
        head = f"{who} declined the merge card" if who else "the merge card was declined"
        return _scrub(head + declined["rest"], names)
    if _CODE.fullmatch(text):
        return text.replace("-", " ").replace("_", " ")
    return _scrub(text, names)


def stop_words(m: Mission, now: datetime, names: Names | None = None) -> str:
    """While a stop drains: "stop asked for by Rich at 14:20:05", or without the name."""
    if not m.stopping or m.stop_requested_at is None:
        return ""
    who = _name(m.stop_requested_by, names)
    return "stop asked for" + (f" by {who}" if who else "") + f" at {vm.fmt_wall(m.stop_requested_at, now=now)}"


def detail_words(m: Mission, now: datetime, names: Names | None = None) -> str:
    """What follows the status: the stop while it drains, or the reason once closed."""
    if m.stopping:
        return stop_words(m, now, names)
    return reason_words(m.closed_reason, now, names) if m.closed else ""


def sentence(m: Mission, now: datetime, names: Names | None = None) -> str:
    """The mission sentence on a feature's page: "Part of mission m-1 (Notes): the mission is
    not merged (stopped at 14:20:05; 2 landed on the branch, 1 did not)."."""
    detail = detail_words(m, now, names)
    return (f"Part of mission {m.id}" + (f" ({m.title})" if m.title else "")
            + f": the mission is {status_words(m)}" + (f" ({detail})" if detail else "") + ".")
