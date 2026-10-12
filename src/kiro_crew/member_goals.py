"""A crewmate's goal record: one long-term goal and the short-term goals toward it.

The record is the user's word on what the crewmate works toward and the
crewmate's working plan to get there (RFC "Crewmate goals"):

* ``long_term`` -- one paragraph. The user changes it directly (the Goals tab);
  the crewmate only proposes a new one, through a ``crewmate.goal`` change card
  the user confirms (``set_goal`` on ``kirocrew-mate``).
* ``key_results`` -- at most :data:`MAX_KEY_RESULTS` readings that say how far
  the crewmate is from the goal, each ``{id, text, target, current, updated_at}``.
  The crewmate proposes the whole set through a ``crewmate.key_results`` card
  (``set_key_results``); once confirmed it records readings directly
  (``key_result_progress``). The user edits or removes them on the Goals tab.
* ``short_term`` -- at most :data:`MAX_SHORT_TERM` steps, each
  ``{id, text, status, updated_at, updated_by}``. Both the user and the crewmate
  change them directly; every change records who made it and when, and the
  crewmate's newest change can be taken back from the Goals tab.
* ``schedules`` -- the three check-in switches (Daily Goal Reminder, Weekly
  report, Monthly review), the one shared time of day, the weekday and the day of
  the month; the gateway schedules they drive are owned by
  :mod:`kiro_crew.dashboard.goal_reminder`.
* ``reports`` -- pointers to the newest weekly reports and monthly reviews, each
  saved as an artifact under ``Crewmates/<name>/Reports``.

Where it lives
--------------
``trust/member-goals/<slug>.json``, beside the permanent rules, NOT in the
agent-writable ``members/<slug>/`` directory that holds ``briefing.md``. The
long-term goal needs the user's button to change, and a file the crewmate's own
file tools can write would let it change the goal without one. The protection is
exactly ``member-rules/``'s: ``trust/`` is in the sensitive-path floor, so the
agent's file-write and edit tools are refused there (the tool gate). It is not
an OS seal: ``trust/`` is a read-write leaf of the crew sandbox, so a shell
command in a sandboxed turn can still write it, the same as the rules file. It
is still a different file from ``member-rules/``: rules are the user's safety
boundary, and nothing about a goal, not even an applied card, ever writes them.

Keyed like every lossy-slug member file: the payload records the exact member
name (and its stable ``member_id`` when it has one), and a read whose name does
not match answers "no goal" rather than another member's record.

Blocking file IO throughout: async callers go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: Directory under ``trust/`` holding one ``<slug>.json`` per member.
GOALS_DIR_NAME = "member-goals"

#: Bounds. The record is injected into every session of the crewmate, so each
#: part is capped on WRITE (refused, never truncated) and the whole stays small.
LONG_TERM_MAX_CHARS = 2000
SHORT_TEXT_MAX_CHARS = 300
MAX_SHORT_TERM = 20
MAX_KEY_RESULTS = 3
KR_TEXT_MAX_CHARS = 200
#: A target or a reading: a number ("27", "80%") or a word ("yes").
KR_VALUE_MAX_CHARS = 40
#: The record file itself, as read back: a hand-edited file past this is
#: unreadable rather than loaded whole.
_RECORD_MAX_BYTES = 64 * 1024


def _now() -> float:
    """The record's clock: every timestamp it writes reads this one function."""
    return time.time()


STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
STATUSES = (STATUS_OPEN, STATUS_DONE, STATUS_DROPPED)

BY_USER = "user"
BY_CREWMATE = "crewmate"
_AUTHORS = (BY_USER, BY_CREWMATE)

DEFAULT_REMINDER_TIME = "09:00"
#: The three check-ins, each its own switch and its own schedule.
CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_MONTHLY = "monthly"
CADENCES = (CADENCE_DAILY, CADENCE_WEEKLY, CADENCE_MONTHLY)
#: Report kinds: what a weekly and a monthly check-in each produce.
REPORT_KINDS = (CADENCE_WEEKLY, CADENCE_MONTHLY)
#: Cron day of week (0 = Sunday); Monday by default.
DEFAULT_WEEKDAY = 1
#: Day of the month, capped at 28 so every month has it.
DEFAULT_MONTH_DAY = 1
MAX_MONTH_DAY = 28
#: How long a key result's readings are kept, and how many at most.
READINGS_KEEP_SECS = 90 * 24 * 3600
MAX_READINGS = 200
#: How many report pointers the record keeps.
MAX_REPORTS = 12
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ID_RE = re.compile(r"^g[0-9a-f]{8}$")
_KR_ID_RE = re.compile(r"^k[0-9a-f]{8}$")

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class GoalError(ValueError):
    """A refused goal write: ``code`` is machine-readable, the text is sayable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GoalsUnreadable(RuntimeError):
    """The record exists but cannot be read; a write must not paper over it."""


def _lock_for(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(slug, threading.Lock())


def member_goals_path(slug: str) -> Path:
    """Absolute path to one member's goal record, containment-checked."""
    from kiro_crew.members import MemberSlugError, validate_slug

    validate_slug(slug)
    root = (data_home() / "trust" / GOALS_DIR_NAME).resolve()
    target = (root / f"{slug}.json").resolve()
    if target.parent != root and root not in target.parents:
        raise MemberSlugError(f"member slug {slug!r} escapes {root}")
    return target


def default_schedules() -> dict[str, Any]:
    return {
        "time": DEFAULT_REMINDER_TIME,
        "timezone": "",
        CADENCE_DAILY: False,
        CADENCE_WEEKLY: False,
        "weekday": DEFAULT_WEEKDAY,
        CADENCE_MONTHLY: False,
        "month_day": DEFAULT_MONTH_DAY,
    }


def empty_record() -> dict[str, Any]:
    """The record of a crewmate that has no goal yet (every existing crewmate)."""
    return {
        "long_term": "",
        "long_term_updated_at": 0.0,
        "long_term_updated_by": "",
        "key_results": [],
        "short_term": [],
        "schedules": default_schedules(),
        "reports": [],
        "last_crewmate_change": None,
        "removed_key_result": None,
    }


def _clean_text(raw: Any, *, max_chars: int, multiline: bool, what: str) -> str:
    from kiro_crew.members import _strip_controls

    if not isinstance(raw, str):
        raise GoalError("invalid_text", f"{what} must be text")
    text = _strip_controls(raw, keep_newlines=multiline).strip()
    if len(text) > max_chars:
        raise GoalError("too_long", f"{what} is longer than {max_chars} characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise GoalError(
            "invalid_text", f"{what} contains characters that cannot be saved"
        ) from None
    return text


def clean_text(raw: Any, *, max_chars: int, multiline: bool, what: str) -> str:
    """*raw* normalised exactly as the record stores it (:class:`GoalError` if refused)."""
    return _clean_text(raw, max_chars=max_chars, multiline=multiline, what=what)


def _valid_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
        and item.get("status") in STATUSES
        and isinstance(item.get("updated_at"), (int, float))
        and item.get("updated_by") in _AUTHORS
    )


def _valid_kr(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("text"), str)
        and isinstance(item.get("target"), str)
        and isinstance(item.get("current"), str)
        and isinstance(item.get("updated_at"), (int, float))
    )


def _valid_reading(r: Any) -> bool:
    return (
        isinstance(r, dict)
        and isinstance(r.get("value"), str)
        and isinstance(r.get("at"), (int, float))
    )


def _prune_readings(readings: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    keep = [r for r in readings if now - float(r["at"]) <= READINGS_KEEP_SECS]
    return keep[-MAX_READINGS:]


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    rec = empty_record()
    if isinstance(data.get("long_term"), str):
        rec["long_term"] = data["long_term"][:LONG_TERM_MAX_CHARS]
    if isinstance(data.get("long_term_updated_at"), (int, float)):
        rec["long_term_updated_at"] = float(data["long_term_updated_at"])
    if data.get("long_term_updated_by") in _AUTHORS:
        rec["long_term_updated_by"] = data["long_term_updated_by"]
    krs = data.get("key_results")
    if isinstance(krs, list):
        rec["key_results"] = []
        for i in [k for k in krs if _valid_kr(k)][:MAX_KEY_RESULTS]:
            kr = dict(i)
            raw = i.get("readings")
            kr["readings"] = [
                {"value": r["value"], "at": float(r["at"])}
                for r in (raw if isinstance(raw, list) else [])
                if _valid_reading(r)
            ][-MAX_READINGS:]
            rec["key_results"].append(kr)
    items = data.get("short_term")
    if isinstance(items, list):
        rec["short_term"] = [dict(i) for i in items if _valid_item(i)][:MAX_SHORT_TERM]
    sched = data.get("schedules")
    if isinstance(sched, dict):
        out = rec["schedules"]
        for cadence in CADENCES:
            out[cadence] = sched.get(cadence) is True
        if isinstance(sched.get("time"), str) and _TIME_RE.match(sched["time"]):
            out["time"] = sched["time"]
        if isinstance(sched.get("timezone"), str):
            out["timezone"] = sched["timezone"][:64]
        if isinstance(sched.get("weekday"), int) and 0 <= sched["weekday"] <= 6:
            out["weekday"] = sched["weekday"]
        day = sched.get("month_day")
        if isinstance(day, int) and 1 <= day <= MAX_MONTH_DAY:
            out["month_day"] = day
    reports = data.get("reports")
    if isinstance(reports, list):
        rec["reports"] = [
            dict(r)
            for r in reports
            if isinstance(r, dict)
            and r.get("kind") in REPORT_KINDS
            and isinstance(r.get("slug"), str)
            and isinstance(r.get("title"), str)
            and isinstance(r.get("at"), (int, float))
        ][-MAX_REPORTS:]
    change = data.get("last_crewmate_change")
    if isinstance(change, dict) and isinstance(change.get("id"), str):
        rec["last_crewmate_change"] = change
    removed = data.get("removed_key_result")
    if (
        isinstance(removed, dict)
        and _valid_kr(removed.get("kr"))
        and isinstance(removed.get("index"), int)
        and isinstance(removed.get("at"), (int, float))
    ):
        kr = dict(removed["kr"])
        raw = kr.get("readings")
        kr["readings"] = [
            {"value": r["value"], "at": float(r["at"])}
            for r in (raw if isinstance(raw, list) else [])
            if _valid_reading(r)
        ][-MAX_READINGS:]
        rec["removed_key_result"] = {
            "kr": kr,
            "index": removed["index"],
            "at": float(removed["at"]),
        }
    return rec


def _read_raw(slug: str) -> dict[str, Any] | None:
    path = member_goals_path(slug)
    try:
        raw = read_bytes_with_retry(path, max_bytes=_RECORD_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GoalsUnreadable(f"goal record for {slug!r} cannot be read") from exc
    if len(raw) > _RECORD_MAX_BYTES:
        raise GoalsUnreadable(f"goal record for {slug!r} is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise GoalsUnreadable(f"goal record for {slug!r} is malformed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("member"), str):
        raise GoalsUnreadable(f"goal record for {slug!r} is malformed")
    return data


def _owned_by(data: dict[str, Any], slug: str, member: str) -> bool:
    # Name-scoped like the rules: a colliding slug's file holds another exact
    # name's goal, which for THIS member is "no goal yet". A stable member id
    # equal to the slug is the member itself, whatever its name is now.
    return data.get("member_id") == slug or data.get("member") == member


def read_goals(slug: str, member: str) -> dict[str, Any]:
    """*member*'s goal record, or :func:`empty_record` when it has none.

    Raises :class:`GoalsUnreadable` for a record that exists and cannot be read,
    so a writer never overwrites it and a reader can say so.
    """
    data = _read_raw(slug)
    if data is None or not _owned_by(data, slug, member):
        return empty_record()
    return _normalize(data)


def _payload(slug: str, member: str, rec: dict[str, Any]) -> str:
    """The exact text :func:`_write` stores for *rec*."""
    from kiro_crew.members import _stable_member_slug

    payload = {
        "member": member,
        "member_id": slug if _stable_member_slug(slug, member) else "",
        "slug": slug,
        **rec,
    }
    return json.dumps(payload, ensure_ascii=False)


def _write(slug: str, member: str, rec: dict[str, Any]) -> None:
    path = member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _dir in (path.parent, path.parent.parent):
        try:
            platform_compat.restrict_dir_to_owner(_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", _dir, exc_info=True)
    atomic_write(path, _payload(slug, member, rec), fsync=True)
    fsync_dir(path.parent, best_effort=True)


def _mutate(slug: str, member: str, fn: Any) -> Any:
    """Read, change and write *member*'s record under its lock; returns ``fn``'s value."""
    with _lock_for(slug):
        data = _read_raw(slug)
        if data is not None and not _owned_by(data, slug, member):
            # Another exact name's record under a colliding slug: never written
            # over from here. The owner's routes refuse ambiguous slugs first.
            raise GoalError("slug_collision", "another crewmate's goal is stored under this name")
        rec = _normalize(data) if data is not None else empty_record()
        result = fn(rec)
        if _fit(slug, member, rec) and isinstance(result, dict) and "record" in result:
            result["record"] = copy.deepcopy(rec)
        _write(slug, member, rec)
        return result


def _fit(slug: str, member: str, rec: dict[str, Any]) -> bool:
    """Prune the oldest readings until *rec* fits :data:`_RECORD_MAX_BYTES`.

    The read cap and this budget are the same number, so every record written
    is readable. Readings are the only part that grows without a count cap the
    user sees; when the record is still too big with none left, the write is
    refused (``record_too_large``). Returns whether anything was pruned.
    """
    pruned = False
    while len(_payload(slug, member, rec).encode("utf-8")) > _RECORD_MAX_BYTES:
        oldest = None
        for kr in rec["key_results"]:
            if kr.get("readings") and (
                oldest is None or kr["readings"][0]["at"] < oldest["readings"][0]["at"]
            ):
                oldest = kr
        removed = rec.get("removed_key_result")
        if isinstance(removed, dict) and removed["kr"].get("readings"):
            if oldest is None or removed["kr"]["readings"][0]["at"] < oldest["readings"][0]["at"]:
                oldest = removed["kr"]
        if oldest is None:
            raise GoalError("record_too_large", "the goal record is too large to save")
        oldest["readings"].pop(0)
        pruned = True
    return pruned


def _new_id(rec: dict[str, Any]) -> str:
    taken = {i["id"] for i in rec["short_term"]}
    while True:
        gid = "g" + secrets.token_hex(4)
        if gid not in taken:
            return gid


def _find(rec: dict[str, Any], goal_id: Any) -> dict[str, Any]:
    if not isinstance(goal_id, str) or not _ID_RE.match(goal_id):
        raise GoalError("invalid_id", "that is not a short-term goal id")
    for item in rec["short_term"]:
        if item["id"] == goal_id:
            return item
    raise GoalError("goal_not_found", "no short-term goal has that id")


def _note_crewmate_change(
    rec: dict[str, Any], op: str, before: dict[str, Any] | None, after: dict[str, Any]
) -> str:
    change_id = "c" + secrets.token_hex(6)
    rec["last_crewmate_change"] = {
        "id": change_id,
        "op": op,
        "goal_id": after["id"],
        "before": copy.deepcopy(before),
        "after": copy.deepcopy(after),
        "at": after["updated_at"],
    }
    return change_id


def set_long_term(
    slug: str, member: str, text: Any, *, by: str, expected: str | None = None
) -> dict[str, Any]:
    """Replace the long-term goal. Returns ``{"before", "after", "record"}``.

    *expected* is a compare-and-set: the write is refused (``changed_meanwhile``)
    when the stored goal is no longer that text, so a card's Apply or Undo never
    overwrites a newer edit. Clearing the goal also turns the reminder off, since
    a crewmate with no goal has nothing to be reminded of.
    """
    clean = _clean_text(text, max_chars=LONG_TERM_MAX_CHARS, multiline=True, what="the goal")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        before = rec["long_term"]
        # The page holds the scrubbed text it was shown (public_view).
        if expected is not None and expected not in (before, scrub_text(before)):
            raise GoalError("changed_meanwhile", "the goal was changed since this was shown")
        rec["long_term"] = clean
        rec["long_term_updated_at"] = _now()
        rec["long_term_updated_by"] = by
        if not clean:
            for cadence in CADENCES:
                rec["schedules"][cadence] = False
        return {"before": before, "after": clean, "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def key_result_shape(items: Any) -> list[dict[str, str]]:
    """The ``{text, target}`` of each key result: what a set's compare-and-set compares."""
    return [
        {"text": str(i.get("text", "")), "target": str(i.get("target", ""))}
        for i in (items or [])
        if isinstance(i, dict)
    ]


def _clean_kr_items(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise GoalError("invalid_key_results", "key results must be a list")
    if len(items) > MAX_KEY_RESULTS:
        raise GoalError("too_many_key_results", f"at most {MAX_KEY_RESULTS} key results")
    out: list[dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise GoalError("invalid_key_results", "each key result is {text, target, current?}")
        text = _clean_text(
            raw.get("text"), max_chars=KR_TEXT_MAX_CHARS, multiline=False, what="a key result"
        )
        target = _clean_text(
            raw.get("target"), max_chars=KR_VALUE_MAX_CHARS, multiline=False, what="a target"
        )
        current = _clean_text(
            raw.get("current", ""), max_chars=KR_VALUE_MAX_CHARS, multiline=False, what="a reading"
        )
        if not text or not target:
            raise GoalError("invalid_key_results", "each key result needs a text and a target")
        if any((k["text"], k["target"]) == (text, target) for k in out):
            # A key result is known by its text and target: two the same would
            # share one id and one set of readings.
            raise GoalError("duplicate_key_result", "two key results have the same text and target")
        out.append({"text": text, "target": target, "current": current})
    return out


def set_key_results(slug: str, member: str, items: Any, *, expected: Any = None) -> dict[str, Any]:
    """Replace the whole set of key results. Returns ``{"before", "record"}``.

    *expected* is the ``{text, target}`` list (:func:`key_result_shape`) of the
    set this write replaces; the write is refused (``changed_meanwhile``) when the
    stored set no longer has that shape, so a card never overwrites a newer edit.
    A key result kept with the same text and target keeps its id and reading.
    """
    clean = _clean_kr_items(items)

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        before = copy.deepcopy(rec["key_results"])
        shown = key_result_shape([_scrub_kr(k) for k in before])
        if expected is not None and key_result_shape(expected) not in (
            key_result_shape(before),
            shown,
        ):
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if clean and not rec["long_term"]:
            raise GoalError("no_goal", "set a goal before its key results")
        kept = {(k["text"], k["target"]): k for k in before}
        taken = {k["id"] for k in before}
        now = _now()
        out = []
        for item in clean:
            old = kept.get((item["text"], item["target"]))
            if old is not None and not item["current"]:
                out.append(dict(old))
                continue
            kid = old["id"] if old is not None else _new_kr_id(taken)
            taken.add(kid)
            readings = list(old.get("readings") or []) if old is not None else []
            if item["current"]:
                readings.append({"value": item["current"], "at": now})
            out.append(
                {
                    "id": kid,
                    **item,
                    "updated_at": now if item["current"] else 0.0,
                    "readings": _prune_readings(readings, now),
                }
            )
        rec["key_results"] = out
        # Undo of a removal covers only the set it was removed from.
        rec["removed_key_result"] = None
        return {"before": before, "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def restore_key_results(
    slug: str, member: str, records: Any, *, expected: Any = None
) -> dict[str, Any]:
    """Put back a whole earlier set of key results, ids and readings included.

    A key-results card's Undo: *records* are the records the card read before it
    applied (scrubbed, since they travel in the card's public plan), *expected*
    the ``{text, target}`` set it applied, so the same compare-and-set as
    :func:`set_key_results` refuses a newer edit. A key result the card kept is
    still in the set under its id: it stays exactly as it is now, readings
    recorded since the card included. Only the ones the card removed come back
    from *records*.
    """
    if not isinstance(records, list) or len(records) > MAX_KEY_RESULTS:
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    if not all(_valid_kr(r) for r in records):
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    if len({r["id"] for r in records}) != len(records) or not all(
        _KR_ID_RE.match(r["id"]) for r in records
    ):
        raise GoalError("invalid_key_results", "the key results to restore are malformed")
    clean = _normalize({"key_results": records})["key_results"]

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        before = copy.deepcopy(rec["key_results"])
        shown = key_result_shape([_scrub_kr(k) for k in before])
        if expected is not None and key_result_shape(expected) not in (
            key_result_shape(before),
            shown,
        ):
            raise GoalError(
                "changed_meanwhile", "the key results were changed since this was shown"
            )
        if clean and not rec["long_term"]:
            raise GoalError("no_goal", "set a goal before its key results")
        now = _now()
        live = {k["id"]: k for k in rec["key_results"]}
        rec["key_results"] = [
            (
                copy.deepcopy(live[k["id"]])
                if k["id"] in live
                else {**k, "readings": _prune_readings(k["readings"], now)}
            )
            for k in clean
        ]
        rec["removed_key_result"] = None
        return {"before": before, "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def remove_key_result(slug: str, member: str, kr_id: Any) -> dict[str, Any]:
    """Remove one key result, keeping it (with its readings) for one Undo.

    Only the newest removal is kept; :func:`restore_key_result` puts it back
    while the set has room and holds no key result with the same text and
    target, and any later write to the set (:func:`set_key_results`) ends it.
    """

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        index = next((n for n, k in enumerate(rec["key_results"]) if k["id"] == kr_id), None)
        if index is None:
            raise GoalError("key_result_not_found", "no key result has that id")
        kr = rec["key_results"].pop(index)
        rec["removed_key_result"] = {"kr": copy.deepcopy(kr), "index": index, "at": _now()}
        return {"removed": copy.deepcopy(kr), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def restore_key_result(slug: str, member: str, kr_id: Any) -> dict[str, Any]:
    """Put back the key result :func:`remove_key_result` removed, readings and all."""

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        removed = rec.get("removed_key_result")
        if not isinstance(removed, dict) or removed["kr"]["id"] != kr_id:
            raise GoalError("undo_unavailable", "that removal can no longer be undone")
        kr = removed["kr"]
        shape = (kr["text"], kr["target"])
        if (
            not rec["long_term"]
            or len(rec["key_results"]) >= MAX_KEY_RESULTS
            or any((k["text"], k["target"]) == shape for k in rec["key_results"])
            or any(k["id"] == kr["id"] for k in rec["key_results"])
        ):
            rec["removed_key_result"] = None
            raise GoalError("stale_undo", "the key results changed since; nothing was undone")
        index = min(int(removed["index"]), len(rec["key_results"]))
        rec["key_results"].insert(index, copy.deepcopy(kr))
        rec["removed_key_result"] = None
        return {"restored": copy.deepcopy(kr), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def _new_kr_id(taken: set[str]) -> str:
    while True:
        kid = "k" + secrets.token_hex(4)
        if kid not in taken:
            return kid


def record_key_result(slug: str, member: str, kr_id: Any, current: Any) -> dict[str, Any]:
    """Record a new reading for one key result (the crewmate's, no sign-off)."""
    if not isinstance(kr_id, str) or not _KR_ID_RE.match(kr_id):
        raise GoalError("invalid_id", "that is not a key result id")
    reading = _clean_text(current, max_chars=KR_VALUE_MAX_CHARS, multiline=False, what="a reading")
    if not reading:
        raise GoalError("empty_text", "a reading needs a value")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        for item in rec["key_results"]:
            if item["id"] == kr_id:
                now = _now()
                item["current"] = reading
                item["updated_at"] = now
                item["readings"] = _prune_readings(
                    [*item.get("readings", []), {"value": reading, "at": now}], now
                )
                return {"item": dict(item), "record": copy.deepcopy(rec)}
        raise GoalError("key_result_not_found", "no key result has that id")

    return _mutate(slug, member, fn)


def add_short_term(slug: str, member: str, text: Any, *, by: str) -> dict[str, Any]:
    """Add one open short-term goal. Returns ``{"item", "change_id"?, "record"}``.

    At :data:`MAX_SHORT_TERM` the oldest finished (done or dropped) goal makes
    room, so checking goals off never jams the list; when all of them are still
    open the add is refused (``too_many_open``).
    """
    clean = _clean_text(text, max_chars=SHORT_TEXT_MAX_CHARS, multiline=False, what="the goal")
    if not clean:
        raise GoalError("empty_text", "a short-term goal needs some text")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        items = rec["short_term"]
        if len(items) >= MAX_SHORT_TERM:
            finished = [i for i in items if i["status"] != STATUS_OPEN]
            if not finished:
                raise GoalError(
                    "too_many_open",
                    f"there are already {MAX_SHORT_TERM} open short-term goals; "
                    "finish or drop one first",
                )
            oldest = min(finished, key=lambda i: i["updated_at"])
            items.remove(oldest)
        item = {
            "id": _new_id(rec),
            "text": clean,
            "status": STATUS_OPEN,
            "updated_at": _now(),
            "updated_by": by,
        }
        items.append(item)
        out: dict[str, Any] = {"item": dict(item)}
        if by == BY_CREWMATE:
            out["change_id"] = _note_crewmate_change(rec, "add", None, item)
        out["record"] = copy.deepcopy(rec)
        return out

    return _mutate(slug, member, fn)


def update_short_term(
    slug: str,
    member: str,
    goal_id: Any,
    *,
    by: str,
    status: Any = None,
    text: Any = None,
) -> dict[str, Any]:
    """Mark one short-term goal done/dropped/open, or reword it."""
    if status is None and text is None:
        raise GoalError("no_change", "give a status or a text")
    if status is not None and status not in STATUSES:
        raise GoalError("invalid_status", "status must be one of " + ", ".join(STATUSES))
    clean = None
    if text is not None:
        clean = _clean_text(text, max_chars=SHORT_TEXT_MAX_CHARS, multiline=False, what="the goal")
        if not clean:
            raise GoalError("empty_text", "a short-term goal needs some text")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        item = _find(rec, goal_id)
        before = dict(item)
        if status is not None:
            item["status"] = status
        if clean is not None:
            item["text"] = clean
        if item["status"] == before["status"] and item["text"] == before["text"]:
            return {"item": dict(item), "changed": False, "record": copy.deepcopy(rec)}
        item["updated_at"] = _now()
        item["updated_by"] = by
        out: dict[str, Any] = {"item": dict(item), "changed": True}
        if by == BY_CREWMATE:
            out["change_id"] = _note_crewmate_change(rec, "update", before, item)
        out["record"] = copy.deepcopy(rec)
        return out

    return _mutate(slug, member, fn)


def remove_short_term(slug: str, member: str, goal_id: Any) -> dict[str, Any]:
    """Delete one short-term goal (the user's action only)."""

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        item = _find(rec, goal_id)
        rec["short_term"].remove(item)
        return {"item": dict(item), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def undo_crewmate_change(slug: str, member: str, change_id: Any) -> dict[str, Any]:
    """Take back the crewmate's newest short-term change, named by *change_id*.

    Only while the goal still reads exactly as the crewmate left it: an edit
    made since (by either side) answers ``stale_undo`` and nothing is written.
    """

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        change = rec.get("last_crewmate_change")
        if not isinstance(change, dict) or change.get("id") != change_id:
            raise GoalError("undo_unavailable", "that change can no longer be undone")
        after = change.get("after") or {}
        current = next((i for i in rec["short_term"] if i["id"] == change.get("goal_id")), None)
        if current is None or current != after:
            raise GoalError("stale_undo", "that goal changed since; nothing was undone")
        if change.get("op") == "add":
            rec["short_term"].remove(current)
        else:
            before = change.get("before")
            if not _valid_item(before):
                raise GoalError("undo_unavailable", "that change can no longer be undone")
            current.clear()
            current.update(before)
        rec["last_crewmate_change"] = None
        return {"undone": change.get("op"), "record": copy.deepcopy(rec)}

    return _mutate(slug, member, fn)


def set_schedules(slug: str, member: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Change the check-in switches and their timing. Returns the record.

    *changes* holds any of ``daily``/``weekly``/``monthly`` (bool), ``time``
    (``HH:MM``), ``timezone`` (IANA name), ``weekday`` (0-6, 0 = Sunday) and
    ``month_day`` (1-28). Turning any switch on needs a long-term goal
    (``no_goal``).
    """
    allowed = {*CADENCES, "time", "timezone", "weekday", "month_day"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise GoalError("invalid_schedule", "give switches or timing to change")
    for cadence in CADENCES:
        if cadence in changes and not isinstance(changes[cadence], bool):
            raise GoalError("invalid_schedule", f"{cadence} must be true or false")
    if "time" in changes and (
        not isinstance(changes["time"], str) or not _TIME_RE.match(changes["time"])
    ):
        raise GoalError("invalid_time", "time must be HH:MM (24-hour)")
    if "timezone" in changes and (
        not isinstance(changes["timezone"], str) or len(changes["timezone"]) > 64
    ):
        raise GoalError("invalid_timezone", "timezone must be an IANA name")
    weekday = changes.get("weekday", 0)
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise GoalError("invalid_weekday", "weekday must be 0 (Sunday) to 6")
    day = changes.get("month_day", 1)
    if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= MAX_MONTH_DAY:
        raise GoalError("invalid_month_day", f"month_day must be 1 to {MAX_MONTH_DAY}")

    def fn(rec: dict[str, Any]) -> dict[str, Any]:
        if not rec["long_term"] and any(changes.get(c) is True for c in CADENCES):
            raise GoalError("no_goal", "set a goal before turning a check-in on")
        rec["schedules"].update(changes)
        return copy.deepcopy(rec)

    return _mutate(slug, member, fn)


def record_report(slug: str, member: str, *, kind: str, artifact: str, title: str) -> None:
    """Remember a saved report so the Goals and Dashboard tabs can list it."""
    if kind not in REPORT_KINDS:
        raise GoalError("invalid_report_kind", "a report is weekly or monthly")

    def fn(rec: dict[str, Any]) -> None:
        rec["reports"] = [
            *rec["reports"],
            {"kind": kind, "slug": artifact, "title": title[:200], "at": _now()},
        ][-MAX_REPORTS:]

    _mutate(slug, member, fn)


def key_result_movement(rec: dict[str, Any], *, since: float) -> list[dict[str, Any]]:
    """Per key result: its reading at the start of the window and now, from ``readings``.

    The start is the last reading at or before *since* (else the first in the
    window); ``None`` when the key result was never read in or before it.
    """
    out = []
    for kr in rec["key_results"]:
        readings = sorted(kr.get("readings") or [], key=lambda r: r["at"])
        before = [r for r in readings if r["at"] <= since]
        inside = [r for r in readings if r["at"] > since]
        start = before[-1] if before else (inside[0] if inside else None)
        out.append(
            {
                "id": kr["id"],
                "text": kr["text"],
                "target": kr["target"],
                "start": start["value"] if start else None,
                "now": kr["current"] or None,
                "readings_in_window": len(inside),
            }
        )
    return out


def _removed_view(removed: Any) -> dict[str, Any] | None:
    if not isinstance(removed, dict):
        return None
    kr = removed["kr"]
    return {"id": kr["id"], "text": scrub_text(kr["text"]), "at": removed["at"]}


def scrub_text(text: Any) -> str:
    """*text* through the dashboard's credential and exfiltration-URL scrubbers.

    Every goal string can be the crewmate's own writing, so each one that leaves
    the record for a page, a tool result or a report goes through here.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    return redact_credentials(redact_exfiltration_urls(str(text or ""))[0])[0]


def _scrub_kr(kr: dict[str, Any]) -> dict[str, Any]:
    out = dict(kr)
    for key in ("text", "target", "current"):
        out[key] = scrub_text(kr.get(key, ""))
    out["readings"] = [
        {**r, "value": scrub_text(r.get("value", ""))} for r in kr.get("readings") or []
    ]
    return out


def public_view(rec: dict[str, Any]) -> dict[str, Any]:
    """The record as the Goals tab and ``goal_read`` see it: open goals first, scrubbed."""
    order = {STATUS_OPEN: 0, STATUS_DONE: 1, STATUS_DROPPED: 2}
    items = sorted(
        ({**i, "text": scrub_text(i["text"])} for i in rec["short_term"]),
        key=lambda i: (order[i["status"]], -float(i["updated_at"])),
    )
    change = rec.get("last_crewmate_change")
    undo = None
    if isinstance(change, dict):
        undo = {
            "id": change.get("id"),
            "op": change.get("op"),
            "goal_id": change.get("goal_id"),
            "text": scrub_text((change.get("after") or {}).get("text", "")),
            "at": change.get("at"),
        }
    return {
        "long_term": scrub_text(rec["long_term"]),
        "long_term_updated_at": rec["long_term_updated_at"] or None,
        "long_term_updated_by": rec["long_term_updated_by"] or None,
        "key_results": [_scrub_kr(k) for k in rec["key_results"]],
        "short_term": items,
        "schedules": dict(rec["schedules"]),
        "reports": sorted(
            ({**r, "title": scrub_text(r["title"])} for r in rec["reports"]),
            key=lambda r: -float(r["at"]),
        ),
        "last_crewmate_change": undo,
        "removed_key_result": _removed_view(rec.get("removed_key_result")),
        "limits": {
            "long_term_max_chars": LONG_TERM_MAX_CHARS,
            "short_text_max_chars": SHORT_TEXT_MAX_CHARS,
            "max_short_term": MAX_SHORT_TERM,
            "max_key_results": MAX_KEY_RESULTS,
            "key_result_text_max_chars": KR_TEXT_MAX_CHARS,
            "key_result_value_max_chars": KR_VALUE_MAX_CHARS,
        },
    }


#: Cap on the goal block injected into the crewmate's context: the long-term
#: goal plus open short-term goals, so a full list never crowds the prompt.
CONTEXT_MAX_CHARS = 4000


def context_block(rec: dict[str, Any]) -> str:
    """The goal text for the member section, or ``""`` when there is no goal.

    The long-term goal, its key results with their latest readings, and the
    open short-term goals.
    """
    lines: list[str] = []
    if rec["long_term"]:
        lines.append("Long-term goal: " + rec["long_term"])
    if rec["key_results"]:
        lines.append("Key results (id: text — reading / target):")
        lines.extend(
            f"- {k['id']}: {k['text']} — {k['current'] or 'not read yet'} / {k['target']}"
            for k in rec["key_results"]
        )
    open_items = [i for i in rec["short_term"] if i["status"] == STATUS_OPEN]
    if open_items:
        lines.append("Open short-term goals (id: text):")
        lines.extend(f"- {i['id']}: {i['text']}" for i in open_items)
    text = "\n".join(lines)
    if len(text) > CONTEXT_MAX_CHARS:
        text = text[:CONTEXT_MAX_CHARS].rstrip() + "\n[... goals cut at their cap]"
    return text
