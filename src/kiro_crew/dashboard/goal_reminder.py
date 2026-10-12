"""A crewmate's check-ins: Daily Goal Reminder, Weekly report, Monthly review.

The Goals tab's three switches (``PUT /api/members/{slug}/goals/schedules``) are
stored in the goal record (:mod:`kiro_crew.member_goals`), and each switch keeps
exactly ONE gateway-owned schedule in step with it, through the cron store's
installer key (``managed_by``, ``goal-<cadence>:<slug>``), so no switch can ever
have two schedules and none touches another's:

* on -> that cadence's old schedule (if any) is removed and a fresh one created
  at the shared time of day: daily ``M H * * *``, weekly ``M H * * <weekday>``,
  monthly ``M H <day> * *``, in the user's timezone;
* off -> that cadence's schedule is removed.

When a schedule fires, the gateway's cron callback hands the job here
(:func:`fire`) instead of running it as an ordinary cron turn: the prompt is
built from the CURRENT goal record and dispatched as one gateway-authored turn
in the crewmate's pinned thread (:func:`mate_welcome.dispatch_hidden_turn`), so
the user sees only the crewmate's reply. That turn is not one the user sent, so
``update_self`` and the cards refuse inside it; the goal tools (readings,
short-term goals, ``save_goal_report``) are admitted there on purpose
(``handlers.member_goals``). One exception: the Monthly review is dispatched
with the crewmate it reviews recorded on the turn by the gateway
(``goal_review_member``), so that turn may raise that crewmate's own
``crewmate.goal`` and ``crewmate.key_results`` cards (``set_goal``,
``set_key_results``). Nothing changes until the user presses the card; the
Daily reminder and the Weekly report still raise none.

The weekly and monthly turns end with ``save_goal_report``: the gateway saves the
report as an artifact under ``Crewmates/<name>/Reports`` (:func:`save_report`),
remembers it on the goal record, and sends one notification linking it; the
crewmate's reply in the thread is one short line, never the report itself.

A check-in that fires with the thread closed, a turn already running, its switch
off or no goal left does nothing that time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_crew import member_goals
from kiro_crew import members as members_mod

logger = logging.getLogger(__name__)

#: Installer key prefix per cadence; the rest is the member slug.
MANAGED_PREFIXES = {c: f"goal-{c}:" for c in member_goals.CADENCES}

#: Who the cron store records as removing a check-in schedule.
_ACTOR = "dashboard"
_SOURCE = "goal_checkins"

#: Where reports are filed, under the crewmate's display name.
REPORTS_FOLDER = "Crewmates/{name}/Reports"

#: How far back the weekly report and the monthly review look at readings.
_WINDOW_SECS = {
    member_goals.CADENCE_WEEKLY: 7 * 24 * 3600,
    member_goals.CADENCE_MONTHLY: 31 * 24 * 3600,
}


def managed_key(slug: str, cadence: str = member_goals.CADENCE_DAILY) -> str:
    return MANAGED_PREFIXES[cadence] + members_mod.validate_slug(slug)


def cadence_of(job: Any) -> str | None:
    key = getattr(job, "managed_by", "")
    if not isinstance(key, str):
        return None
    for cadence, prefix in MANAGED_PREFIXES.items():
        if key.startswith(prefix):
            return cadence
    return None


def is_reminder_job(job: Any) -> bool:
    """Whether *job* is one of a crewmate's goal check-ins (daily, weekly, monthly).

    The one predicate every list that leaves them out reads: the Goals tab's
    switches are their only control, so ``GET /api/crons`` (the Schedule page and
    a crewmate's Schedules tab, which filters that same list) omits them while
    the cron store keeps running them.
    """
    return cadence_of(job) is not None


def cron_expr_for(cadence: str, schedules: dict[str, Any]) -> str:
    """The cron expression for *cadence* at the record's shared time."""
    hour, minute = (int(x) for x in str(schedules["time"]).split(":"))
    if cadence == member_goals.CADENCE_WEEKLY:
        return f"{minute} {hour} * * {int(schedules['weekday'])}"
    if cadence == member_goals.CADENCE_MONTHLY:
        return f"{minute} {hour} {int(schedules['month_day'])} * *"
    return f"{minute} {hour} * * *"


async def reminder_jobs(crons: Any, slug: str, cadence: str | None = None) -> list[Any]:
    keys = (
        {managed_key(slug, cadence)}
        if cadence
        else {managed_key(slug, c) for c in member_goals.CADENCES}
    )
    return [j for j in await crons.list_jobs_async(include_disabled=True) if j.managed_by in keys]


_NAMES = {
    member_goals.CADENCE_DAILY: "Daily goal reminder",
    member_goals.CADENCE_WEEKLY: "Weekly report",
    member_goals.CADENCE_MONTHLY: "Monthly review",
}


async def sync_schedule(crons: Any, slug: str, label: str, schedules: dict[str, Any]) -> None:
    """Make the store hold exactly the schedules *schedules* asks for, one per switch.

    For each cadence the old schedule goes first, then a fresh one with a new id
    when its switch is on: the cron store's installer contract replaces a managed
    job that way (no replace-in-place), so a run in flight only removes a dead one.
    """
    from kiro_crew.cron_service.schedule import get_local_tz

    tz = schedules.get("timezone") or get_local_tz()[0]
    for cadence in member_goals.CADENCES:
        for job in await reminder_jobs(crons, slug, cadence):
            await crons.remove_job_async(job.id, actor=_ACTOR, source=_SOURCE)
        if not schedules.get(cadence):
            continue
        await crons.add_managed_job_async(
            managed_key(slug, cadence),
            name=f"{_NAMES[cadence]}: {label}"[:200],
            message=f"{_NAMES[cadence]} for {label}. It runs in the crewmate's own chat.",
            cron_expr=cron_expr_for(cadence, schedules),
            timezone=tz,
            silent=True,
            hide_in_chat=True,
            persistent_session=False,
            minimal_context=True,
        )


_PREAMBLE = (
    "Kiro Crew sends this; the user did not type it and sees only your reply, so "
    "keep this note itself out of what you write. Write in the language of the "
    "dashboard's [UI LANGUAGE] when it is shown, else English.\n\n"
)


def _movement_lines(rec: dict[str, Any], cadence: str) -> str:
    since = time.time() - _WINDOW_SECS[cadence]
    lines = []
    for m in member_goals.key_result_movement(rec, since=since):
        start = m["start"] if m["start"] is not None else "not read"
        now = m["now"] if m["now"] is not None else "not read"
        lines.append(
            f"- {m['text']}: {start} -> {now} (target {m['target']}, "
            f"{m['readings_in_window']} readings in this period)"
        )
    return "\n".join(lines) or "(no key results yet)"


def _short_term_changes(rec: dict[str, Any], cadence: str) -> str:
    since = time.time() - _WINDOW_SECS[cadence]
    recent = [i for i in rec["short_term"] if float(i["updated_at"]) > since]
    return (
        "\n".join(f"- [{i['status']}] {i['text']} (by {i['updated_by']})" for i in recent)
        or "(none changed in this period)"
    )


def reminder_prompt(rec: dict[str, Any], cadence: str = member_goals.CADENCE_DAILY) -> str:
    """The hidden turn's text for *cadence*, built from the current goal record."""
    block = member_goals.context_block(rec)
    if cadence == member_goals.CADENCE_DAILY:
        return (
            "[Daily goal reminder] " + _PREAMBLE + f"{block}\n\n"
            "Take a fresh reading of each key result and record it with "
            "key_result_progress, then lead your message with those readings and say "
            "briefly where things stand toward the long-term goal. Keep your short-term "
            "goals current: check off the ones you finished and drop the ones that no "
            "longer apply with short_goal_update, and add the next steps with "
            "short_goal_add (all on the kirocrew-mate server). The user sees those "
            "changes on your Goals tab and can undo them, so act on what you are "
            "confident of. Ask the user something only when you are blocked."
        )
    if cadence == member_goals.CADENCE_WEEKLY:
        return (
            "[Weekly report] "
            + _PREAMBLE
            + f"{block}\n\nHow each key result moved this week, from its readings:\n"
            + _movement_lines(rec, cadence)
            + "\n\nShort-term goals changed this week:\n"
            + _short_term_changes(rec, cadence)
            + "\n\nWrite this week's report for the user in Markdown: how each key "
            "result moved, the short-term goals done and added, what is stuck and why, "
            "and the plan for next week. Base the numbers on the readings above, and "
            "take a fresh reading first when one is stale. Save it with "
            'save_goal_report (kind "weekly"), then reply in the thread with one short '
            "line pointing at it; the report itself stays out of the chat."
        )
    return (
        "[Monthly review] "
        + _PREAMBLE
        + f"{block}\n\nHow each key result moved this month, from its readings:\n"
        + _movement_lines(rec, cadence)
        + "\n\nReview whether the long-term goal and its key results still fit the "
        "work you are actually doing. Write the review in Markdown: what the month "
        "showed, whether the direction still holds, and what you would change. Save "
        'it with save_goal_report (kind "monthly"), then reply in the thread with one '
        "short line pointing at it. When the key results or the goal no longer fit the "
        "work, propose the replacement as a card in this turn (set_key_results for the "
        "whole set, set_goal for the goal) with the reason drawn from the review; the "
        "user decides with the card's button. When they still fit, leave them as they "
        "are and propose nothing."
    )


async def fire(state: Any, job: Any) -> str:
    """Run one check-in for *job*'s crewmate; returns a one-line outcome for the run log."""
    from kiro_crew.dashboard.mate_welcome import dispatch_hidden_turn

    cadence = cadence_of(job)
    if cadence is None:
        return "skipped: not a goal check-in"
    slug = str(job.managed_by)[len(MANAGED_PREFIXES[cadence]) :]
    if state is None:
        return "skipped: the dashboard is not running"
    try:
        members_mod.validate_slug(slug)
        binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)
    except (members_mod.MemberSlugError, OSError):
        return "skipped: no crewmate thread"
    if not binding:
        return "skipped: no crewmate thread"
    member = binding.get("member")
    if not isinstance(member, str) or not member:
        return "skipped: no crewmate thread"
    try:
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
    except member_goals.GoalsUnreadable:
        logger.warning("goal check-in for %s: goal record unreadable", slug)
        return "skipped: goal record unreadable"
    if not rec["schedules"].get(cadence) or not rec["long_term"]:
        return "skipped: switch off or no goal"
    slot = state.get_slot(binding.get("slot_key", ""))
    if (
        slot is None
        or getattr(slot, "mode", "") != members_mod.DM_SLOT_MODE
        or getattr(slot, "agent", "") != member
        or getattr(slot, "executor", "") == "remote"
    ):
        return "skipped: the crewmate's thread is not open"
    if slot.running or getattr(slot, "_queue", None):
        return "skipped: the crewmate is busy"
    review = member if cadence == member_goals.CADENCE_MONTHLY else ""
    dispatch_hidden_turn(
        state, slot, reminder_prompt(rec, cadence), human_seen=False, goal_review_member=review
    )
    return "started"


def _folder_label(label: str) -> str:
    # A path segment: "/" would nest folders, so it reads as a dash.
    clean = label.replace("/", "-").strip() or "crewmate"
    return clean[:100]


def save_report(
    slug: str, member: str, label: str, *, kind: str, title: str, markdown: str, session_key: str
) -> dict[str, Any]:
    """Save one report as a Markdown artifact under ``Crewmates/<name>/Reports``.

    Records it on the goal record and returns ``{"slug", "title", "url",
    "folder"}``. Blocking IO.
    """
    from kiro_crew.artifacts import get_default_folder_store, get_default_store

    if kind not in member_goals.REPORT_KINDS:
        raise member_goals.GoalError("invalid_report_kind", "a report is weekly or monthly")
    if not isinstance(markdown, str) or not markdown.strip():
        raise member_goals.GoalError("empty_report", "the report needs some text")
    # The crewmate wrote the report: scrubbed like every goal string it writes.
    title = member_goals.scrub_text(title)
    markdown = member_goals.scrub_text(markdown)
    folder_path = REPORTS_FOLDER.format(name=_folder_label(label))
    folder_id = get_default_folder_store().resolve_path(folder_path, create_missing=True)
    art = get_default_store().create(
        name=title,
        content=markdown,
        kind="markdown",
        source="chat",
        description=f"{_NAMES[kind]} by {label}",
        tags=["goal-report", kind],
        folder_id=folder_id,
        session_key=session_key,
    )
    member_goals.record_report(slug, member, kind=kind, artifact=art.slug, title=title)
    return {
        "slug": art.slug,
        "title": title,
        "url": f"/artifacts/{art.slug}",
        "folder": folder_path,
    }
