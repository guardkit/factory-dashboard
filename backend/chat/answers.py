"""One fixed plain-English template per query (design 2026-10-17, requirement 9).

The answer is written here from a query's `Result.fields` and nothing else: no model text, and no
figure that is not a field. The templates themselves hold no digits, so every number in an answer
came from the record (checked again at run time by `grounding.figures_grounded`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from backend.chat.tools import Result

CANT_ANSWER = (
    "I can't answer that from the dashboard's records — I can tell you about a feature, a project, "
    "issues, deliveries, cost or time."
)


def _s(fields: dict[str, object], key: str) -> str:
    value = fields.get(key, "")
    return value if isinstance(value, str) else ""


def _list(fields: dict[str, object], key: str) -> list[object]:
    value = fields.get(key, [])
    return list(value) if isinstance(value, list) else []


def _join(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return "; ".join(items[:-1]) + "; and " + items[-1]


def _join_or(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return "; ".join(items[:-1]) + "; or " + items[-1]


def _plural(count: str, one: str, many: str) -> str:
    return f"{count} {one if count == '1' else many}"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith((".", "?", "!", "…")) else text + "."


def _for_project(fields: dict[str, object]) -> str:
    project = _s(fields, "project")
    return f" in {project}" if project else ""


def _more(fields: dict[str, object], key: str, where: str) -> str:
    more = _s(fields, key)
    return f" And {more} more on the {where}." if more and more != "0" else ""


def _hidden(fields: dict[str, object]) -> str:
    """The pages' own sentence for the older unresolved failures a list leaves out, or ""."""
    return _sentence(_s(fields, "hidden_line"))


def _stale(fields: dict[str, object]) -> str:
    note = _s(fields, "stale_note")
    return f"Note: {_sentence(note)} " if note else ""


# --- one template per query --------------------------------------------------------------------------


def _feature_status(fields: dict[str, object]) -> str:
    head = _s(fields, "label").rstrip(".") + (f", in {_s(fields, 'project')}" if _s(fields, "project") else "")
    if _s(fields, "delivered_on"):
        parts = [f"{head}: merged and checked on {_s(fields, 'delivered_on')}."]
    else:
        where = (f"It stopped at the {_s(fields, 'station')} step." if _s(fields, "stopped")
                 else f"How far: {_s(fields, 'station')}.")
        parts = [f"{head}: {_sentence(_s(fields, 'status_text'))}", where]
    verdict = _s(fields, "verdict")
    reason = _s(fields, "verdict_reason")
    if verdict == "not measured yet":
        parts.append("Whether it is on track is not measured yet.")
    else:
        parts.append(verdict[:1].upper() + verdict[1:] + (f": {_sentence(reason)}" if reason else "."))
    parts.append(f"Sent {_s(fields, 'sent')}; elapsed {_s(fields, 'elapsed')}; "
                 f"{_plural(_s(fields, 'attempts'), 'build attempt', 'build attempts')}.")
    issues = [str(i) for i in _list(fields, "open_issues")]
    if issues:
        parts.append("Open issues: " + _sentence(_join(issues)))
    if _s(fields, "mission_line"):
        parts.append(_sentence(_s(fields, "mission_line")))
    parts.append(_sentence(_s(fields, "cost")))
    return _stale(fields) + " ".join(parts)


def _project_summary(fields: dict[str, object]) -> str:
    parts = [f"{_s(fields, 'project')}: {_s(fields, 'in_progress')} in progress; "
             f"{_s(fields, 'delivered_7d')} merged and checked in the last seven days, "
             f"{_s(fields, 'delivered_total')} in total."]
    if _s(fields, "period_label"):
        parts.append(f"Merged and checked {_s(fields, 'period_label')}: {_s(fields, 'period_delivered')}.")
    count = _s(fields, "open_issue_count")
    if count == "0":
        parts.append("No open issues.")
    else:
        listed = [str(i) for i in _list(fields, "open_issues")]
        parts.append(f"{_plural(count, 'open issue', 'open issues')}"
                     + (": " + _sentence(_join(listed)) if listed else "."))
        parts.append(_more(fields, "more_issues", "project page").strip())
        parts.append(_hidden(fields))
    parts.append(_sentence(_s(fields, "cost")))
    return _stale(fields) + " ".join(p for p in parts if p)


def _open_issues(fields: dict[str, object]) -> str:
    count = _s(fields, "count")
    if count == "0":
        return _stale(fields) + f"There are no open issues{_for_project(fields)}."
    rows = []
    for row in _list(fields, "rows"):
        if not isinstance(row, dict):
            continue
        detail = f" ({row.get('detail')})" if row.get("detail") else ""
        rows.append(f"{row.get('kind')}: {row.get('subject')}{detail}, open {row.get('age')}, "
                    f"{row.get('how_serious')}")
    verb = "is" if count == "1" else "are"
    head = f"There {verb} {_plural(count, 'open issue', 'open issues')}{_for_project(fields)}"
    body = (": " + _sentence(_join(rows)) + _more(fields, "more", "Issues page")) if rows else "."
    hidden = _hidden(fields)
    return _stale(fields) + head + body + (" " + hidden if hidden else "")


def _delivered(fields: dict[str, object]) -> str:
    count = _s(fields, "count")
    when = _s(fields, "period_label")
    if count == "0":
        text = f"Nothing was merged and checked{_for_project(fields)} {when}."
    else:
        rows = [f"{r.get('label')} on {r.get('on')}" for r in _list(fields, "rows") if isinstance(r, dict)]
        text = (f"{_plural(count, 'feature was', 'features were')} merged and checked{_for_project(fields)} "
                f"{when}: " + _sentence(_join(rows)) + _more(fields, "more", "Delivered page"))
    if _s(fields, "other_endings"):
        text += " " + _sentence(_s(fields, "other_endings"))
    return _stale(fields) + text


def _weekly_report(fields: dict[str, object]) -> str:
    when = _s(fields, "period_label")
    if _s(fields, "withheld"):
        return (f"The weekly report, {when}, is withheld: the factory's record was last read at "
                f"{_s(fields, 'withheld_since')}, so its figures could be out of date.")
    scope = f" for {_s(fields, 'project')}" if _s(fields, "project") else ""
    parts = [f"Weekly report{scope}, {when}: "
             f"{_plural(_s(fields, 'delivered_count'), 'feature', 'features')} merged and checked, "
             f"{_s(fields, 'in_flight_count')} in progress."]
    delivered = [str(d) for d in _list(fields, "delivered")]
    if delivered:
        parts.append("Delivered: " + _sentence(_join(delivered)) + _more(fields, "more_delivered", "report"))
    if not _s(fields, "project"):
        if _s(fields, "plan_agreed"):
            parts.append(f"Against the plan: {_s(fields, 'behind')} behind, {_s(fields, 'on_target')} on target, "
                         f"{_s(fields, 'ahead')} ahead.")
        else:
            parts.append("No plan has been agreed yet, so nothing is shown as on target.")
        issues = (f"Issues: {_s(fields, 'issues_opened')} opened and {_s(fields, 'issues_closed')} closed "
                  "in the period")
        if _s(fields, "oldest_open"):
            issues += f"; the oldest still open is {_s(fields, 'oldest_open')} old"
        parts.append(issues + ".")
    else:
        parts.append("The plan and issue figures on the report cover every project, so they are not split "
                     "out here; the project page lists its issues.")
    parts.append(_sentence(_s(fields, "cost")))
    return " ".join(parts)


def _not_measured(fields: dict[str, object]) -> str:
    if _s(fields, "what") == "cost":
        return ("Cost isn't measured yet. It is being added to the dashboard; until then every page shows "
                "cost as not measured, never as zero.")
    return ("Time split into machine time and waiting isn't measured yet. Each feature's elapsed time is on "
            "its page, and you can ask me about a feature by name.")


#: The clarifying questions, one fixed sentence per kind the model may ask for (grounding.CLARIFY_KINDS).
CLARIFY_QUESTIONS: dict[str, str] = {
    "which_feature": "Which feature do you mean, by its name or its code on the Features page?",
    "which_project": "Which project do you mean?",
    "which_period": "Which dates do you mean, such as this week, last week or last month?",
    "rephrase": "Could you ask that another way, naming the feature or the project?",
}
_CLARIFY_WITH_OPTIONS = {"which_feature": "Which feature do you mean: ", "which_project": "Which project do you mean: "}


def clarify_question(kind: str, option_labels: Sequence[str] = ()) -> str:
    """The question shown for a clarification: the kind's own sentence or, with options, the
    options' labels as the records give them."""
    if option_labels:
        return _CLARIFY_WITH_OPTIONS[kind] + _join_or(list(option_labels)) + "?"
    return CLARIFY_QUESTIONS[kind]


TEMPLATES: dict[str, Callable[[dict[str, object]], str]] = {
    "feature_status": _feature_status,
    "project_summary": _project_summary,
    "open_issues": _open_issues,
    "delivered": _delivered,
    "weekly_report": _weekly_report,
    "cost_summary": _not_measured,
    "time_summary": _not_measured,
}


def render(result: Result) -> str:
    """The answer for a query's result. Every status has exactly one wording."""
    fields = result.fields
    if result.status == "not_found":
        kind = _s(fields, "kind") or "record"
        name = _s(fields, "name")
        # "couldn't find", not "there is no": the search can miss a record the user named differently.
        return (f"I couldn't find a {kind} matching “{name}”." if name else f"I couldn't find that {kind}.")
    if result.status == "ambiguous":
        listed = [str(x) for x in _list(fields, "listed")]
        more = _s(fields, "more")
        tail = f" (and {more} more)" if more and more != "0" else ""
        name = _s(fields, "name")
        what = f"“{name}”" if name else "that name"
        if _s(fields, "partial"):
            kind = _s(fields, "kind") or "record"
            head = (f"I couldn't find a {kind} matching “{name}” exactly." if name
                    else f"I couldn't find a {kind} matching that exactly.")
            return f"{head} Did you mean {_join_or(listed)}{tail}?"
        return (f"{_s(fields, 'count')} {_s(fields, 'kind')}s match {what}: "
                f"{_join(listed)}{tail}. Which one do you mean?")
    if result.status == "bad_period":
        return ("I couldn't tell which dates you mean. Try “this week”, “last week”, “last month”, or two "
                "dates such as the ones on the Delivered page.")
    if result.status == "not_measured":
        return _not_measured(fields)
    return TEMPLATES[result.query](fields)
