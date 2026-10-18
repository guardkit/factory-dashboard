"""The queries the model may choose from, with their arguments (design 2026-10-17, requirement 9).

Operator accounts get all seven. Client-tenant accounts get no chat (the October design keeps the
July client views hidden), so `queries_for` returns nothing for them and the route refuses them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.auth import ResolvedUser
from backend.chat import tools


@dataclass(frozen=True)
class Arg:
    name: str
    required: bool
    meaning: str


@dataclass(frozen=True)
class QuerySpec:
    name: str
    purpose: str
    args: tuple[Arg, ...]
    run: Callable[..., tools.Result]

    def arg_names(self) -> frozenset[str]:
        return frozenset(a.name for a in self.args)


_PERIOD = Arg("period", False, "a period: " + ", ".join(f'"{p}"' for p in tools.PERIOD_PHRASES)
              + '; leave it out for "this week"')
_PROJECT = Arg("project", False, "a project name as the user said it")

QUERIES: dict[str, QuerySpec] = {
    q.name: q for q in (
        QuerySpec("feature_status", "where one feature stands: how far through, on track, issues",
                  (Arg("feature", True, "the feature's title words or its code (FEAT-…), as the user said it"),),
                  tools.feature_status),
        QuerySpec("project_summary", "one project's headline: in progress, delivered, open issues",
                  (Arg("project", True, "a project name as the user said it"), _PERIOD), tools.project_summary),
        QuerySpec("open_issues", "the open issues, for every project or one", (_PROJECT,), tools.open_issues),
        QuerySpec("delivered", "what was merged and checked in a period", (_PERIOD, _PROJECT), tools.delivered),
        QuerySpec("weekly_report", "the weekly report's headline for a period", (_PERIOD, _PROJECT),
                  tools.weekly_report),
        QuerySpec("cost_summary", "model cost of a feature or project",
                  (_PROJECT, Arg("feature", False, "a feature as the user said it"), _PERIOD), tools.cost_summary),
        QuerySpec("time_summary", "machine time and waiting time of a feature or project",
                  (_PROJECT, Arg("feature", False, "a feature as the user said it"), _PERIOD), tools.time_summary),
    )
}


def queries_for(user: ResolvedUser) -> dict[str, QuerySpec]:
    """The queries this account may ask: all of them for an operator, none for a client tenant."""
    return QUERIES if user.is_operator else {}
