"""Crewmate goals: the record, the owner's Goals tab routes, the crewmate's tools,
the ``crewmate.goal`` card, the Daily Goal Reminder and the context block.

The rules the RFC sets and these tests pin:

* a crewmate changes only its OWN goals (the caller's slot decides, never a body);
* short-term goals are a direct write (no card), the long-term goal is a card;
* at most 20 short-term goals; the record is name-scoped like the rules file;
* each check-in switch (daily, weekly, monthly) keeps exactly its own one
  schedule, and none can turn on with no goal;
* key results are proposed as a whole set on a card, readings are direct and kept;
* a weekly or monthly report is filed as an artifact and linked, not pasted;
* nothing about a goal is ever written to ``member-rules/``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from guide_route_helpers import FakeState, agent, run_guide_app

from kiro_crew import change_card_catalog as catalog
from kiro_crew import mcp_mate, member_goals
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.config.paths import data_home
from kiro_crew.cron import CronService
from kiro_crew.dashboard import goal_reminder, mate_welcome
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.handlers import member_goals as goal_routes
from kiro_crew.dashboard.handlers import member_self as self_routes

DEFAULT = "kirocrew"
ORDINARY = "scout"

READ = ("POST", "/api/member/self/goals/read", goal_routes.api_member_self_goals_read)
SHORT = ("POST", "/api/member/self/goals/short-term", goal_routes.api_member_self_short_goal)
PROPOSE = ("POST", "/api/member/self/propose", self_routes.api_member_self_propose)
OWNER_ROUTES = [
    ("GET", "/api/members/{slug}/goals", goal_routes.api_member_goals_get),
    ("PUT", "/api/members/{slug}/goals/long-term", goal_routes.api_member_goals_long_term),
    ("POST", "/api/members/{slug}/goals/short-term", goal_routes.api_member_goals_short_add),
    (
        "PATCH",
        "/api/members/{slug}/goals/short-term/{goal_id}",
        goal_routes.api_member_goals_short_update,
    ),
    ("POST", "/api/members/{slug}/goals/undo", goal_routes.api_member_goals_undo),
    ("PUT", "/api/members/{slug}/goals/schedules", goal_routes.api_member_goals_schedules),
    ("PUT", "/api/members/{slug}/goals/key-results", goal_routes.api_member_goals_key_results),
    (
        "DELETE",
        "/api/members/{slug}/goals/key-results/{kr_id}",
        goal_routes.api_member_goals_key_result_delete,
    ),
    (
        "POST",
        "/api/members/{slug}/goals/key-results/{kr_id}/restore",
        goal_routes.api_member_goals_key_result_restore,
    ),
]
KR = ("POST", "/api/member/self/goals/key-result", goal_routes.api_member_self_key_result)
REPORT = ("POST", "/api/member/self/goals/report", goal_routes.api_member_self_goal_report)


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    _floor_monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


@pytest.fixture(autouse=True)
def _owner_ok(_floor_monkeypatch):
    async def allow(_request, _operation):
        return None

    _floor_monkeypatch.setattr(goal_routes, "require_owner_dashboard_request", allow)


def _seed() -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {
        DEFAULT: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ASSISTANT_MEMBER_NAME: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ORDINARY: KiroCrewAgentConfig(kiro_agent=DEFAULT, display_name="Scout"),
    }
    cfg.default_agent = DEFAULT
    cfg.save()
    _invalidate_config_cache()


def _slug(member: str) -> str:
    _invalidate_config_cache()
    return members_mod.member_slug(member)


def _thread(state: FakeState, member: str, *, user_sent: bool = True):
    slug = _slug(member)
    key = members_mod.DM_SLOT_KEY_PREFIX + slug
    members_mod.write_dm_binding(slug, member=member, slot_key=key)
    slot = state.open_slot(key)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = member
    slot._turn_user_sent = user_sent
    return slot


def _call(state: FakeState, route, body: dict, key: str) -> tuple[int, dict]:
    method, path, _ = route

    async def go(c):
        r = await c.request(method, path, json=body, headers=agent(f"dashboard:{key}"))
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[route])


class _Crons:
    """A real cron store in the test's data home, served on the app's loop."""

    def __init__(self, base: Path) -> None:
        self.svc = CronService(base_dir=base)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.svc, name)


def _owner(state: FakeState, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    async def go(c):
        r = await c.request(method, path, json=body, headers={"X-Test-Auth": "owner"})
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=OWNER_ROUTES)


def _rules_dir() -> Path:
    return data_home() / "trust" / members_mod.RULES_DIR_NAME


# ── the record ──


def test_short_term_goals_cap_at_twenty_and_make_room_from_finished_ones(
    _floor_monkeypatch,
) -> None:
    """At 20 the oldest done or dropped goal goes first; all 20 open refuses the add."""
    _seed()
    slug = _slug(ORDINARY)
    clock = [1_000_000_000.0]

    def tick() -> float:
        clock[0] += 1
        return clock[0]

    _floor_monkeypatch.setattr(member_goals, "_now", tick)
    ids = [
        member_goals.add_short_term(slug, ORDINARY, f"step {n}", by="user")["item"]["id"]
        for n in range(member_goals.MAX_SHORT_TERM)
    ]
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.add_short_term(slug, ORDINARY, "one too many", by="user")
    assert exc.value.code == "too_many_open"
    # Finished later in the list but earlier in time: it is the one evicted.
    member_goals.update_short_term(slug, ORDINARY, ids[7], by="user", status="dropped")
    member_goals.update_short_term(slug, ORDINARY, ids[3], by="user", status="done")
    member_goals.add_short_term(slug, ORDINARY, "next", by="crewmate")
    rec = member_goals.read_goals(slug, ORDINARY)
    kept = {i["id"] for i in rec["short_term"]}
    assert len(rec["short_term"]) == member_goals.MAX_SHORT_TERM
    assert ids[7] not in kept and ids[3] in kept
    member_goals.add_short_term(slug, ORDINARY, "after", by="crewmate")
    assert ids[3] not in {i["id"] for i in member_goals.read_goals(slug, ORDINARY)["short_term"]}


def test_the_record_is_name_scoped_and_never_written_over_for_another_name() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Keep the release notes current", by="user")
    # Another exact name that lands on the same slug reads "no goal" ...
    assert member_goals.read_goals(slug, "Scout!")["long_term"] == ""
    # ... and cannot write over this member's record.
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_long_term(slug, "Scout!", "something else", by="user")
    assert exc.value.code == "slug_collision"
    assert member_goals.read_goals(slug, ORDINARY)["long_term"] == "Keep the release notes current"


def test_the_goal_lives_under_trust_and_never_in_the_rules_file() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship it", by="user")
    path = member_goals.member_goals_path(slug)
    assert path.parent == (data_home() / "trust" / member_goals.GOALS_DIR_NAME).resolve()
    assert path.is_file()
    assert not _rules_dir().exists() or not any(_rules_dir().iterdir())
    assert members_mod.member_dir(slug) not in path.parents


def test_a_long_term_write_is_a_compare_and_set() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "first", by="user")
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_long_term(slug, ORDINARY, "second", by="user", expected="stale")
    assert exc.value.code == "changed_meanwhile"
    member_goals.set_long_term(slug, ORDINARY, "second", by="user", expected="first")
    assert member_goals.read_goals(slug, ORDINARY)["long_term"] == "second"


def test_the_crewmates_newest_change_undoes_only_while_unchanged() -> None:
    _seed()
    slug = _slug(ORDINARY)
    added = member_goals.add_short_term(slug, ORDINARY, "draft notes", by="crewmate")
    done = member_goals.update_short_term(
        slug, ORDINARY, added["item"]["id"], by="crewmate", status="done"
    )
    member_goals.undo_crewmate_change(slug, ORDINARY, done["change_id"])
    (item,) = member_goals.read_goals(slug, ORDINARY)["short_term"]
    assert item["status"] == "open"
    again = member_goals.update_short_term(slug, ORDINARY, item["id"], by="crewmate", status="done")
    member_goals.update_short_term(slug, ORDINARY, item["id"], by="user", text="draft the notes")
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.undo_crewmate_change(slug, ORDINARY, again["change_id"])
    assert exc.value.code in ("stale_undo", "undo_unavailable")


@pytest.mark.parametrize("cadence", member_goals.CADENCES)
def test_no_check_in_turns_on_without_a_goal_and_clearing_the_goal_turns_all_off(
    cadence: str,
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_schedules(slug, ORDINARY, {cadence: True})
    assert exc.value.code == "no_goal"
    member_goals.set_long_term(slug, ORDINARY, "Ship it", by="user")
    member_goals.set_schedules(slug, ORDINARY, {c: True for c in member_goals.CADENCES})
    member_goals.set_long_term(slug, ORDINARY, "", by="user")
    sched = member_goals.read_goals(slug, ORDINARY)["schedules"]
    assert not any(sched[c] for c in member_goals.CADENCES)


def test_key_results_cap_at_three_and_keep_ninety_days_of_readings(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship clean releases", by="user")
    four = [{"text": f"kr {n}", "target": "10"} for n in range(4)]
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(slug, ORDINARY, four)
    assert exc.value.code == "too_many_key_results"
    member_goals.set_key_results(slug, ORDINARY, four[:2])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    clock = [1_000_000_000.0]
    _floor_monkeypatch.setattr(member_goals, "_now", lambda: clock[0])
    member_goals.record_key_result(slug, ORDINARY, kid, "3")
    clock[0] += member_goals.READINGS_KEEP_SECS + 1
    member_goals.record_key_result(slug, ORDINARY, kid, "5")
    kr, _other = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert kr["current"] == "5"
    assert [r["value"] for r in kr["readings"]] == ["5"]
    # Re-proposing the same text and target keeps the id and its readings.
    member_goals.set_key_results(slug, ORDINARY, four[:2], expected=four[:2])
    assert member_goals.read_goals(slug, ORDINARY)["key_results"][0]["readings"] == kr["readings"]
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(slug, ORDINARY, four[:1], expected=four[2:])
    assert exc.value.code == "changed_meanwhile"


def test_key_result_movement_reads_the_window_from_readings(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    clock = [1_000_000.0]
    _floor_monkeypatch.setattr(member_goals, "_now", lambda: clock[0])
    member_goals.record_key_result(slug, ORDINARY, kid, "10")
    clock[0] += 10 * 86400
    member_goals.record_key_result(slug, ORDINARY, kid, "14")
    clock[0] += 86400
    member_goals.record_key_result(slug, ORDINARY, kid, "19")
    rec = member_goals.read_goals(slug, ORDINARY)
    (m,) = member_goals.key_result_movement(rec, since=clock[0] - 7 * 86400)
    assert (m["start"], m["now"], m["readings_in_window"]) == ("10", "19", 2)


# ── the crewmate's tools ──


def test_the_crewmate_changes_only_its_own_short_term_goals() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    # A name in the body is ignored: the caller's slot names the crewmate.
    status, body = _call(
        state, SHORT, {"add": "learn the release flow", "member": ORDINARY}, slot.key
    )
    assert status == 200, body
    assert body["item"]["updated_by"] == "crewmate"
    mine = member_goals.read_goals(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME)
    assert [i["text"] for i in mine["short_term"]] == ["learn the release flow"]
    assert member_goals.read_goals(_slug(ORDINARY), ORDINARY)["short_term"] == []


def test_a_thread_the_binding_does_not_name_is_refused() -> None:
    _seed()
    state = FakeState()
    slot = state.open_slot("member-other")
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = ORDINARY
    status, body = _call(state, SHORT, {"add": "x"}, slot.key)
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert member_goals.read_goals(_slug(ORDINARY), ORDINARY)["short_term"] == []


def test_a_subagent_cannot_change_goals() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    method, path, _ = SHORT

    async def go(c):
        r = await c.request(method, path, json={"add": "x"}, headers=agent("subagent:abc"))
        return r.status, await r.json()

    status, body = run_guide_app(go, state, extra_routes=[SHORT])
    assert (status, body["code"]) == (403, "subagent_caller")


def test_the_crewmate_records_a_reading_directly(_floor_monkeypatch) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    slug = _slug(ASSISTANT_MEMBER_NAME)
    member_goals.set_long_term(slug, ASSISTANT_MEMBER_NAME, "Ship", by="user")
    member_goals.set_key_results(slug, ASSISTANT_MEMBER_NAME, [{"text": "tests", "target": "yes"}])
    kid = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["key_results"][0]["id"]
    status, body = _call(state, KR, {"id": kid, "current": "no"}, slot.key)
    assert status == 200, body
    (kr,) = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["key_results"]
    assert kr["current"] == "no" and len(kr["readings"]) == 1


def test_a_report_is_filed_under_the_crewmates_reports_folder_and_linked(
    _floor_monkeypatch,
) -> None:
    from kiro_crew import artifacts as artifacts_mod

    _seed()
    state = FakeState()
    pushed: list[Any] = []

    class Bus:
        def push(self, payload):
            pushed.append(payload)

    state.notification_bus = Bus()  # type: ignore[attr-defined]
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    slug = _slug(ASSISTANT_MEMBER_NAME)
    member_goals.set_long_term(slug, ASSISTANT_MEMBER_NAME, "Ship", by="user")
    md = "# Week 41\n\nPRs described: 14 -> 19 of 27."
    status, body = _call(
        state, REPORT, {"kind": "weekly", "title": "Week 41", "markdown": md}, slot.key
    )
    assert status == 200, body
    art = artifacts_mod.get_default_store().get(body["slug"])
    assert art.kind == "markdown"
    folders = artifacts_mod.get_default_folder_store()
    label = ASSISTANT_MEMBER_NAME.replace("/", "-")
    assert art.folder_id == folders.resolve_path(f"Crewmates/{label}/Reports")
    assert body["url"] == f"/artifacts/{body['slug']}"
    (note,) = pushed
    assert note.url == body["url"]
    assert md not in (note.body or "")
    (report,) = member_goals.read_goals(slug, ASSISTANT_MEMBER_NAME)["reports"]
    assert (report["kind"], report["slug"]) == ("weekly", body["slug"])
    status, body = _call(state, REPORT, {"kind": "daily", "title": "x", "markdown": "y"}, slot.key)
    assert (status, body["code"]) == (400, "invalid_report_kind")


def test_the_reminders_hidden_turn_may_check_goals_off_but_not_raise_a_card(
    _floor_monkeypatch,
) -> None:
    """Short-term goals are admitted on a turn the user did not send; set_goal is not."""
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    status, body = _call(state, SHORT, {"add": "check the CI"}, slot.key)
    assert status == 200, body
    status, body = _call(state, READ, {}, slot.key)
    assert status == 200 and body["short_term"][0]["text"] == "check the CI"
    status, body = _call(
        state, PROPOSE, {"request": "goal", "params": {"long_term": "new"}}, slot.key
    )
    assert (status, body["code"]) == (403, "not_user_turn")


def _capture_proposals(monkeypatch) -> list[dict]:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True})

    monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    return seen


GOAL_ASK = {"request": "goal", "params": {"long_term": "Ship the docs site"}}
KR_ASK = {"request": "key_results", "params": {"items": [{"text": "pages", "target": "40"}]}}


def test_the_monthly_review_raises_its_own_goal_and_key_results_cards(
    _floor_monkeypatch,
) -> None:
    """The gateway's own Monthly review of this crewmate may raise the two goal cards."""
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot._turn_goal_review = ORDINARY
    assert _call(state, PROPOSE, GOAL_ASK, slot.key)[0] == 200
    assert _call(state, PROPOSE, KR_ASK, slot.key)[0] == 200
    assert [c["kind"] for c in seen] == [
        catalog.KIND_CREWMATE_GOAL,
        catalog.KIND_CREWMATE_KEY_RESULTS,
    ]
    assert all(c["params"]["member"] == ORDINARY for c in seen)
    # Nothing else a card could ask for: the model, the approval mode, the tools.
    status, body = _call(state, PROPOSE, {"request": "model", "params": {"model": "x"}}, slot.key)
    assert (status, body["code"]) == (403, "not_user_turn")
    assert len(seen) == 2


@pytest.mark.parametrize(
    "review",
    ["", "daily", "weekly"],
    ids=["ordinary-cron-turn", "daily-reminder", "weekly-report"],
)
def test_a_turn_that_is_not_the_monthly_review_raises_no_goal_card(
    _floor_monkeypatch, review: str
) -> None:
    """The daily and weekly check-ins, and any other turn the user did not send."""
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    if review:
        member_goals.set_long_term(slug, ORDINARY, "Keep docs current", by="user")
        member_goals.set_schedules(slug, ORDINARY, {review: True})
        sent: list[dict] = []
        _floor_monkeypatch.setattr(
            mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append(k)
        )

        class Job:
            managed_by = goal_reminder.managed_key(slug, review)

        slot.task = None
        assert asyncio.run(goal_reminder.fire(state, Job())) == "started"
        assert sent == [{"human_seen": False, "goal_review_member": ""}]
        slot = _thread(state, ORDINARY, user_sent=False)
        slot._turn_goal_review = sent[0]["goal_review_member"]
    for ask in (GOAL_ASK, KR_ASK):
        status, body = _call(state, PROPOSE, ask, slot.key)
        assert (status, body["code"]) == (403, "not_user_turn")
    assert seen == []


def test_the_monthly_review_is_dispatched_naming_its_own_crewmate(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot.task = None
    member_goals.set_long_term(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_schedules(slug, ORDINARY, {"monthly": True})
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    class Job:
        managed_by = goal_reminder.managed_key(slug, "monthly")

    assert asyncio.run(goal_reminder.fire(state, Job())) == "started"
    ((args, kwargs),) = sent
    assert args[1] is slot
    assert kwargs == {"human_seen": False, "goal_review_member": ORDINARY}


def test_a_monthly_review_for_one_crewmate_raises_no_card_in_anothers_thread(
    _floor_monkeypatch,
) -> None:
    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME, user_sent=False)
    slot._turn_goal_review = ORDINARY
    for ask in (GOAL_ASK, KR_ASK):
        status, body = _call(state, PROPOSE, ask, slot.key)
        assert (status, body["code"]) == (403, "not_user_turn")
    assert seen == []


def test_the_monthly_review_cannot_raise_a_card_for_another_crewmate_by_name(
    _floor_monkeypatch,
) -> None:
    """``propose_change`` names its member in the body, so it stays user-turn only."""
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen = _capture_proposals(_floor_monkeypatch)
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, user_sent=False)
    slot._turn_goal_review = ORDINARY
    route = ("POST", "/api/cards/agent/propose", card_routes.api_cards_agent_propose)
    body = {
        "kind": catalog.KIND_CREWMATE_GOAL,
        "params": {"member": ASSISTANT_MEMBER_NAME, "long_term": "Something else"},
    }
    status, out = _call(state, route, body, slot.key)
    assert (status, out["code"]) == (403, "not_user_turn")
    assert seen == []


def test_set_goal_becomes_a_card_for_the_callers_own_member(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        from aiohttp import web

        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True, "card": {"id": "c1"}})

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, _ = _call(
        state, PROPOSE, {"request": "goal", "params": {"long_term": "Help Ray ship"}}, slot.key
    )
    assert status == 200
    assert seen == [
        {
            "kind": catalog.KIND_CREWMATE_GOAL,
            "params": {"member": ASSISTANT_MEMBER_NAME, "long_term": "Help Ray ship"},
        }
    ]
    # Proposing wrote nothing: the goal changes only when the card is applied.
    assert (
        member_goals.read_goals(_slug(ASSISTANT_MEMBER_NAME), ASSISTANT_MEMBER_NAME)["long_term"]
        == ""
    )


def test_the_mate_server_lists_the_goal_tools_and_routes_set_goal_to_a_card() -> None:
    names = {t["name"] for t in mcp_mate._list_tools()}
    assert {
        "goal_read",
        "set_goal",
        "set_key_results",
        "key_result_progress",
        "save_goal_report",
        "short_goal_add",
        "short_goal_update",
    } <= names
    assert mcp_mate._PROPOSALS["set_goal"] == "goal"
    assert mcp_mate._PROPOSALS["set_key_results"] == "key_results"
    assert "key_result_progress" not in mcp_mate._PROPOSALS
    assert self_routes.SELF_PROPOSALS["key_results"] == catalog.KIND_CREWMATE_KEY_RESULTS
    assert "short_goal_add" not in mcp_mate._PROPOSALS
    assert self_routes.SELF_PROPOSALS["goal"] == catalog.KIND_CREWMATE_GOAL
    assert goal_routes.OP_SHORT_GOAL not in guide_routes._USER_TURN_OPERATIONS
    assert "member.propose_self" in guide_routes._USER_TURN_OPERATIONS


# ── the card ──


def test_the_goal_card_writes_the_goal_route_with_a_compare_and_set_and_undoes_it() -> None:
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_GOAL, {"member": ORDINARY, "long_term": " Ship it "}
    )
    assert params == {"member": ORDINARY, "long_term": "Ship it"}
    snapshot = {"exists": True, "slug": "scout", "member_id": "", "long_term": "old"}
    preview = catalog.build_preview(catalog.KIND_CREWMATE_GOAL, params, snapshot, {})
    (step,) = preview["apply"]
    assert (step["method"], step["path"]) == ("PUT", "/api/members/scout/goals/long-term")
    assert "member-rules" not in step["path"]
    assert ("PUT", "/api/members/{slug}/goals/long-term") in catalog.HOOKED_ROUTES
    plan, reason = catalog.build_undo(catalog.KIND_CREWMATE_GOAL, params, snapshot, [{}], 1)
    assert reason is None
    (undo,) = plan
    assert undo["path"] == step["path"]
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_GOAL, params, {**snapshot, "long_term": "Ship it"}, {}
        )
    assert exc.value.code == "no_change"


def test_the_goal_card_body_carries_the_text_it_replaces_as_expected() -> None:
    import json

    params = {"member": ORDINARY, "long_term": "new"}
    snapshot = {"exists": True, "slug": "scout", "long_term": "old"}
    (step,) = catalog.build_preview(catalog.KIND_CREWMATE_GOAL, params, snapshot, {})["apply"]
    body = step["body"] if isinstance(step["body"], dict) else json.loads(step["body"])
    assert body["expected"] == "old"
    (undo,) = catalog.build_undo(catalog.KIND_CREWMATE_GOAL, params, snapshot, [{}], 1)[0]
    ubody = undo["body"] if isinstance(undo["body"], dict) else json.loads(undo["body"])
    assert (ubody["long_term"], ubody["expected"]) == ("old", "new")


def test_the_key_results_card_replaces_the_set_with_a_compare_and_set() -> None:
    import json

    items = [{"text": "PRs described", "target": "27"}]
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items}
    )
    snapshot = {"exists": True, "slug": "scout", "has_goal": True, "key_results": []}
    (step,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, snapshot, {})[
        "apply"
    ]
    assert (step["method"], step["path"]) == ("PUT", "/api/members/scout/goals/key-results")
    body = step["body"] if isinstance(step["body"], dict) else json.loads(step["body"])
    assert (body["items"], body["expected"]) == (items, [])
    (undo,) = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, snapshot, [{}], 1)[0]
    ubody = undo["body"] if isinstance(undo["body"], dict) else json.loads(undo["body"])
    assert (ubody["items"], ubody["expected"]) == ([], items)
    assert ("PUT", "/api/members/{slug}/goals/key-results") in catalog.HOOKED_ROUTES
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(
            catalog.KIND_CREWMATE_KEY_RESULTS, params, {**snapshot, "has_goal": False}, {}
        )
    assert exc.value.code == "no_goal"
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items * 4}
        )


def test_set_key_results_becomes_a_card_for_the_callers_own_member(_floor_monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict] = []

    async def fake_propose(_request, _slot_key, _sk, *, kind, params, reason):
        from aiohttp import web

        seen.append({"kind": kind, "params": params})
        return web.json_response({"ok": True})

    _floor_monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    items = [{"text": "tests pass", "target": "yes"}]
    status, _ = _call(
        state, PROPOSE, {"request": "key_results", "params": {"items": items}}, slot.key
    )
    assert status == 200
    assert seen == [
        {
            "kind": catalog.KIND_CREWMATE_KEY_RESULTS,
            "params": {"member": ASSISTANT_MEMBER_NAME, "items": items},
        }
    ]


# ── the owner's routes and the check-ins ──


def _reminder_state(tmp_path: Path) -> FakeState:
    state = FakeState()
    state.crons = _Crons(tmp_path / "crons")  # type: ignore[attr-defined]
    return state


def _schedules(state: FakeState, slug: str, cadence: str = "daily") -> list[Any]:
    key = goal_reminder.managed_key(slug, cadence)
    return [j for j in state.crons.svc.list_jobs(include_disabled=True) if j.managed_by == key]


def _jobs_by_cadence(state: FakeState, slug: str) -> dict[str, list[Any]]:
    return {c: _schedules(state, slug, c) for c in member_goals.CADENCES}


@pytest.mark.parametrize("cadence", member_goals.CADENCES)
def test_each_switch_creates_and_removes_exactly_its_own_schedule(
    tmp_path: Path, cadence: str
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/schedules"
    status, body = _owner(state, "PUT", path, {"member": ORDINARY, cadence: True})
    assert (status, body["code"]) == (409, "no_goal")
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())
    _owner(
        state,
        "PUT",
        f"/api/members/{slug}/goals/long-term",
        {"member": ORDINARY, "long_term": "Ship"},
    )
    for when in ("09:00", "07:15"):
        status, body = _owner(
            state,
            "PUT",
            path,
            {
                "member": ORDINARY,
                cadence: True,
                "time": when,
                "timezone": "UTC",
                "weekday": 3,
                "month_day": 12,
            },
        )
        assert status == 200, body
        assert body["schedules"][cadence] is True
    jobs = _jobs_by_cadence(state, slug)
    (job,) = jobs[cadence]
    assert all(v == [] for c, v in jobs.items() if c != cadence)
    expected = {
        "daily": "15 7 * * *",
        "weekly": "15 7 * * 3",
        "monthly": "15 7 12 * *",
    }[cadence]
    assert job.schedule.cron_expr == expected
    status, body = _owner(state, "PUT", path, {"member": ORDINARY, cadence: False})
    assert status == 200 and body["schedules"][cadence] is False
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())


def test_turning_one_switch_off_leaves_the_others(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    _owner(
        state,
        "PUT",
        base + "/schedules",
        {"member": ORDINARY, "daily": True, "weekly": True, "monthly": True},
    )
    first = {c: [j.id for j in v] for c, v in _jobs_by_cadence(state, slug).items()}
    assert all(len(v) == 1 for v in first.values())
    _owner(state, "PUT", base + "/schedules", {"member": ORDINARY, "weekly": False})
    after = _jobs_by_cadence(state, slug)
    assert after["weekly"] == []
    assert len(after["daily"]) == 1 and len(after["monthly"]) == 1


def test_clearing_the_goal_removes_every_check_in_schedule(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    _owner(
        state,
        "PUT",
        base + "/schedules",
        {"member": ORDINARY, "daily": True, "weekly": True, "monthly": True},
    )
    status, _ = _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": ""})
    assert status == 200
    assert all(v == [] for v in _jobs_by_cadence(state, slug).values())


def test_the_owner_edits_and_removes_key_results(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    one = [{"text": "PRs described", "target": "27"}]
    status, body = _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": one})
    assert status == 200 and [k["text"] for k in body["key_results"]] == ["PRs described"]
    status, body = _owner(
        state, "PUT", base + "/key-results", {"member": ORDINARY, "items": [], "expected": one}
    )
    assert status == 200 and body["key_results"] == []


def test_a_removed_key_result_comes_back_with_its_readings(tmp_path: Path) -> None:
    """The Goals tab's remove keeps the key result for one Undo, readings and place."""
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    two = [{"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": two})
    first = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    member_goals.record_key_result(slug, ORDINARY, first["id"], "12")
    member_goals.record_key_result(slug, ORDINARY, first["id"], "19")
    kept = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    status, body = _owner(state, "DELETE", f"{base}/key-results/{kept['id']}?member={ORDINARY}")
    assert status == 200, body
    assert [k["text"] for k in body["key_results"]] == ["notes out"]
    assert body["removed_key_result"]["id"] == kept["id"]
    status, body = _owner(
        state, "POST", f"{base}/key-results/{kept['id']}/restore", {"member": ORDINARY}
    )
    assert status == 200, body
    assert body["removed_key_result"] is None
    assert body["key_results"][0] == kept
    assert [r["value"] for r in body["key_results"][0]["readings"]] == ["12", "19"]
    status, body = _owner(
        state, "POST", f"{base}/key-results/{kept['id']}/restore", {"member": ORDINARY}
    )
    assert (status, body["code"]) == (410, "undo_unavailable")


def test_a_later_write_to_the_key_results_ends_the_removal_undo(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals"
    _owner(state, "PUT", base + "/long-term", {"member": ORDINARY, "long_term": "Ship"})
    two = [{"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}]
    _owner(state, "PUT", base + "/key-results", {"member": ORDINARY, "items": two})
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    _owner(state, "DELETE", f"{base}/key-results/{kid}?member={ORDINARY}")
    status, body = _owner(
        state,
        "PUT",
        base + "/key-results",
        {"member": ORDINARY, "items": [{"text": "PRs described", "target": "27"}]},
    )
    assert status == 200 and body["removed_key_result"] is None
    status, body = _owner(state, "POST", f"{base}/key-results/{kid}/restore", {"member": ORDINARY})
    assert (status, body["code"]) == (410, "undo_unavailable")
    assert [k["text"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]] == [
        "PRs described"
    ]


def test_the_schedule_list_leaves_out_goal_check_ins_and_keeps_other_jobs(tmp_path: Path) -> None:
    """GET /api/crons (Schedule page, a crewmate's Schedules tab) omits only check-ins."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import cron as cron_routes

    svc = CronService(base_dir=tmp_path / "crons")
    slug = "scout"

    async def go() -> list[str]:
        await svc.add_job_async("ordinary", "do it", every_secs=3600)
        await svc.add_managed_job_async(
            "app:mochi:feed", name="app job", message="feed", every_secs=3600
        )
        for cadence in member_goals.CADENCES:
            await svc.add_managed_job_async(
                goal_reminder.managed_key(slug, cadence),
                name=f"check-in {cadence}",
                message="x",
                cron_expr="0 9 * * *",
            )
        app = web.Application()
        app["state"] = SimpleNamespace(
            crons=svc,
            owner_id="",
            conversation_log=None,
            push_slots_update=MagicMock(),
            has_slot=lambda _key: False,
        )
        app.router.add_get("/api/crons", cron_routes.api_crons)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/crons")
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        return sorted(j["name"] for j in data["jobs"])

    assert asyncio.run(go()) == ["app job", "ordinary"]
    # Still in the store, still scheduled.
    stored = {j.managed_by for j in svc.list_jobs(include_disabled=True)}
    assert {goal_reminder.managed_key(slug, c) for c in member_goals.CADENCES} <= stored


def test_the_owner_long_term_route_refuses_a_stale_expected(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    path = f"/api/members/{slug}/goals/long-term"
    _owner(state, "PUT", path, {"member": ORDINARY, "long_term": "first"})
    status, body = _owner(
        state, "PUT", path, {"member": ORDINARY, "long_term": "second", "expected": "other"}
    )
    assert (status, body["code"]) == (409, "changed_meanwhile")


def test_the_owner_undoes_the_crewmates_change(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    added = member_goals.add_short_term(slug, ORDINARY, "draft notes", by="crewmate")
    state = _reminder_state(tmp_path)
    status, body = _owner(state, "GET", f"/api/members/{slug}/goals?member={ORDINARY}")
    assert status == 200 and body["last_crewmate_change"]["id"] == added["change_id"]
    status, body = _owner(
        state,
        "POST",
        f"/api/members/{slug}/goals/undo",
        {"member": ORDINARY, "change_id": added["change_id"]},
    )
    assert status == 200 and body["short_term"] == []


# ── the reminder turn and the context ──


def test_the_reminder_fires_one_hidden_turn_in_the_crewmates_own_thread(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = FakeState()
    slot = _thread(state, ORDINARY)
    slot.task = None
    slot._turn_user_sent = False
    sent: list[tuple] = []
    _floor_monkeypatch.setattr(
        mate_welcome, "dispatch_hidden_turn", lambda *a, **k: sent.append((a, k))
    )

    class Job:
        managed_by = goal_reminder.managed_key(slug)

    assert asyncio.run(goal_reminder.fire(state, Job())) == "skipped: switch off or no goal"
    member_goals.set_long_term(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "pages reviewed", "target": "40"}])
    member_goals.add_short_term(slug, ORDINARY, "audit the README", by="crewmate")
    member_goals.set_schedules(slug, ORDINARY, {"daily": True})
    assert asyncio.run(goal_reminder.fire(state, Job())) == "started"
    ((args, kwargs),) = sent
    assert args[1] is slot and kwargs == {"human_seen": False, "goal_review_member": ""}
    prompt = args[2]
    assert "Keep docs current" in prompt and "audit the README" in prompt
    assert "pages reviewed" in prompt and "key_result_progress" in prompt


@pytest.mark.parametrize(
    ("cadence", "marker"),
    [
        ("daily", "[Daily goal reminder]"),
        ("weekly", "[Weekly report]"),
        ("monthly", "[Monthly review]"),
    ],
)
def test_each_check_in_prompt_is_written_as_principles(cadence: str, marker: str) -> None:
    rec = member_goals.empty_record()
    rec["long_term"] = "Keep docs current"
    prompt = goal_reminder.reminder_prompt(rec, cadence)
    assert prompt.startswith(marker)
    for word in ("must", "never", "exactly"):
        assert word not in prompt.lower()
    if cadence != "daily":
        assert "save_goal_report" in prompt and f'kind "{cadence}"' in prompt
    # Only the Monthly review is admitted to raise the goal cards, so only it asks.
    asks = "set_goal" in prompt and "set_key_results" in prompt
    assert asks is (cadence == "monthly")


def test_the_goal_is_injected_after_the_rules_and_before_the_briefing() -> None:
    from kiro_crew.context_assembly.member import build_member_section

    _seed()
    slug = _slug(ORDINARY)
    members_mod.write_member_rules(slug, member=ORDINARY, text="Ask before deleting anything.")
    member_goals.set_long_term(slug, ORDINARY, "Keep docs current", by="user")
    member_goals.add_short_term(slug, ORDINARY, "audit the README", by="crewmate")
    done = member_goals.add_short_term(slug, ORDINARY, "old step", by="user")["item"]["id"]
    member_goals.update_short_term(slug, ORDINARY, done, by="user", status="done")
    section = build_member_section(ORDINARY)
    rules_at = section.index("\n\n[PERMANENT RULES")
    goal_at = section.index("\n\n[YOUR GOAL")
    assert rules_at < goal_at
    assignment = "\n\n[CURRENT ASSIGNMENT"
    assert assignment in section
    assert goal_at < section.index(assignment)
    assert "Keep docs current" in section and "audit the README" in section
    assert "old step" not in section


# ── a crewmate made on a crewmate.create card ──

NEW_MEMBER = "rover"
NEW_MEMBER_ID = "m-rover01"


class _CardState(FakeState):
    """A dashboard state the card hook can publish to, with a real cron store."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        from kiro_crew.dashboard.change_cards import CardStore

        self._change_card_store = CardStore(tmp_path / "cards.json")
        self.crons = _Crons(tmp_path / "crons")
        self.agent_bodies: list[dict[str, Any]] = []
        self.cron_bodies: list[dict[str, Any]] = []

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        self.frames.append((kind, payload))

    def push_refresh(self, kind: str) -> None:
        self.frames.append(("refresh", {"kind": kind}))


def _create_card_app(state: _CardState) -> web.Application:
    from kiro_crew.dashboard.handlers import change_cards as card_routes

    async def create_agent(request: web.Request) -> web.Response:
        # Stands in for POST /api/agents: it records the body and publishes the
        # crewmate with the immutable member_id the real route allocates.
        body = await request.json()
        state.agent_bodies.append(body)
        cfg = KiroCrewConfig.load()
        cfg.agents[NEW_MEMBER] = KiroCrewAgentConfig(
            kiro_agent=DEFAULT,
            member_id=NEW_MEMBER_ID,
            description=body.get("description", ""),
        )
        cfg.save()
        _invalidate_config_cache()
        return web.json_response({"ok": True, "name": NEW_MEMBER, "member_id": NEW_MEMBER_ID})

    async def create_cron(request: web.Request) -> web.Response:
        body = await request.json()
        state.cron_bodies.append(body)
        job = state.crons.svc.add_job(body["name"], body["message"], cron_expr=body["cron"])
        return web.json_response({"ok": True, "id": job.id})

    app = web.Application(middlewares=[_owner_auth, card_routes.change_card_middleware])
    app["state"] = state
    card_routes.register_change_card_routes(app)
    app.router.add_post("/api/agents", create_agent)
    app.router.add_post("/api/crons", create_cron)
    app.router.add_put(
        "/api/members/{slug}/goals/long-term", goal_routes.api_member_goals_long_term
    )
    return app


@web.middleware
async def _owner_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _propose_create(state: _CardState, params: dict[str, Any]) -> dict[str, Any]:
    from kiro_crew.dashboard import change_cards as cards

    params = catalog.validate_params(catalog.KIND_CREWMATE_CREATE, params)
    before = asyncio.run(
        cards.read_state(catalog.KIND_CREWMATE_CREATE, params, [], state=state, app=None)
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_CREATE, params, before, {})
    rec = state._change_card_store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=catalog.KIND_CREWMATE_CREATE,
        params=params,
        reason="make rover",
        preview=preview,
        before=before,
        context={},
    )
    return state._change_card_store.public(rec)


def _fill(value: Any, fills: list[dict[str, Any]], responses: list[Any]) -> Any:
    """The browser's ``runPlan`` fills, for a step's body or its path."""
    for f in fills:
        got = responses[f["step"]][f["key"]]
        if isinstance(value, str):
            value = value.replace(catalog.placeholder(f), got)
        else:
            value = {**value, f["field"]: got}
    return value


def _apply_create(state: _CardState, card: dict[str, Any]) -> list[tuple[int, Any]]:
    """Send the card's apply plan the way the dashboard does, stopping at a refusal."""
    from aiohttp.test_utils import TestClient, TestServer

    async def main() -> list[tuple[int, Any]]:
        client = TestClient(TestServer(_create_card_app(state)))
        await client.start_server()
        out: list[tuple[int, Any]] = []
        responses: list[Any] = []
        try:
            for i, step in enumerate(card["plan"]["apply"]):
                fills = step.get("fill") or []
                path = _fill(step["path"], [f for f in fills if f.get("in") == "path"], responses)
                body = step["body"]
                body_fills = [f for f in fills if f.get("in") != "path"]
                if body_fills:
                    body = _fill(body, body_fills, responses)
                r = await client.request(
                    step["method"],
                    path,
                    json=body,
                    headers={
                        "X-Card-Id": card["id"],
                        "X-Card-Revision": str(card["revision"]),
                        "X-Card-Op": "apply",
                        "X-Card-Step": str(i),
                    },
                )
                data = await r.json()
                out.append((r.status, data))
                responses.append(data)
                if r.status >= 400:
                    break
            from kiro_crew.dashboard.handlers import change_cards as card_routes

            if card_routes._BACKGROUND:
                await asyncio.gather(*list(card_routes._BACKGROUND))
            return out
        finally:
            await client.close()

    return asyncio.run(main())


@pytest.fixture
def _card_owner(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


SCHEDULED = {
    "name": "Rover",
    "goal": "Keep the docs current",
    "schedule": {"cron_expr": "0 9 * * 1"},
}


def test_a_create_card_writes_its_goal_as_the_long_term_goal_not_the_description(
    tmp_path: Path, _card_owner
) -> None:
    _seed()
    state = _CardState(tmp_path)
    card = _propose_create(state, SCHEDULED)
    results = _apply_create(state, card)
    assert [status for status, _ in results] == [200, 200, 200], results
    (agent_body,) = state.agent_bodies
    assert agent_body.get("description", "") == ""
    assert KiroCrewConfig.load().agents[NEW_MEMBER].description == ""
    rec = member_goals.read_goals(NEW_MEMBER_ID, NEW_MEMBER)
    assert rec["long_term"] == "Keep the docs current"
    assert member_goals.member_goals_path(NEW_MEMBER_ID).parent.name == "member-goals"
    assert state._change_card_store.get(card["id"])["status"] == "applied"


def test_a_create_cards_schedule_stays_its_work_schedule(tmp_path: Path, _card_owner) -> None:
    _seed()
    state = _CardState(tmp_path)
    card = _propose_create(state, SCHEDULED)
    _apply_create(state, card)
    (cron_body,) = state.cron_bodies
    assert cron_body == {
        "name": "Rover schedule",
        "message": "Keep the docs current",
        "agent": catalog.DEFAULT_CREWMATE_TEMPLATE,
        "silent": False,
        "hide_in_chat": False,
        "strict_schedule": True,
        "cron": "0 9 * * 1",
        "member_id": NEW_MEMBER_ID,
    }
    (job,) = state.crons.svc.list_jobs(include_disabled=True)
    assert job.managed_by in ("", None)
    assert all(not _schedules(state, NEW_MEMBER_ID, c) for c in member_goals.CADENCES)


def test_a_create_card_whose_goal_write_fails_says_so_on_the_card(
    tmp_path: Path, _card_owner, _floor_monkeypatch
) -> None:
    _seed()
    state = _CardState(tmp_path)

    def broken(*_a: Any, **_kw: Any) -> Any:
        raise OSError("disk full")

    _floor_monkeypatch.setattr(member_goals, "set_long_term", broken)
    card = _propose_create(state, SCHEDULED)
    results = _apply_create(state, card)
    assert [status for status, _ in results] == [200, 500]
    assert results[1][1]["code"] == "goals_write_failed"
    rec = state._change_card_store.get(card["id"])
    assert rec["status"] == "partial"
    assert rec["error"]["step"] == catalog.CREWMATE_CREATE_GOAL_STEP
    assert rec["result"]["summary"] == (
        "Created crewmate “Rover”, but its goal or its schedule was not saved"
    )
    # The schedule after the failed goal never ran, and Undo still removes the crewmate.
    assert state.cron_bodies == []
    assert [s["path"] for s in rec["plan"]["undo"]] == [f"/api/agents/{NEW_MEMBER}"]


def test_a_path_fill_takes_only_the_identifier_the_earlier_step_returned() -> None:
    from kiro_crew.dashboard.handlers.change_cards import match_step

    p = catalog.validate_params(catalog.KIND_CREWMATE_CREATE, {"name": "Rover", "goal": "g"})
    goal = catalog.build_preview(catalog.KIND_CREWMATE_CREATE, p, {"exists": False}, {})["apply"][
        catalog.CREWMATE_CREATE_GOAL_STEP
    ]
    body = {"member": NEW_MEMBER, "long_term": "g", "expected": ""}
    ev = [{"name": NEW_MEMBER, "member_id": NEW_MEMBER_ID}]

    def check(path: str, evidence: list[dict[str, Any]] = ev) -> str | None:
        return match_step(goal, method="PUT", path=path, query={}, body=body, evidence=evidence)

    assert check(f"/api/members/{NEW_MEMBER_ID}/goals/long-term") is None
    assert check("/api/members/scout/goals/long-term") == "path"
    bad = [{"name": NEW_MEMBER, "member_id": "../scout"}]
    assert check("/api/members/../scout/goals/long-term", bad) == "fill:slug"


def test_deleting_a_crewmate_removes_its_goal_check_ins(tmp_path: Path, _floor_monkeypatch) -> None:
    """Its check-ins are listed nowhere else, so the delete has to take them."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    _seed()
    gone, kept = _slug(ORDINARY), _slug(ASSISTANT_MEMBER_NAME)
    svc = CronService(base_dir=tmp_path / "crons")

    async def go() -> int:
        await svc.add_job_async("ordinary", "do it", every_secs=3600)
        for slug in (gone, kept):
            for cadence in member_goals.CADENCES:
                await svc.add_managed_job_async(
                    goal_reminder.managed_key(slug, cadence),
                    name=f"{slug} {cadence}",
                    message="x",
                    cron_expr="0 9 * * *",
                )
        request = MagicMock(spec=web.Request)
        request.method = "DELETE"
        request.match_info = {"name": ORDINARY}
        request.app = {"state": SimpleNamespace(crons=svc, push_refresh=MagicMock())}
        request.get = lambda key, default=None: default
        return (await api_kirocrew_agent_delete(request)).status

    assert asyncio.run(go()) == 200
    names = sorted(j.name for j in svc.list_jobs(include_disabled=True))
    assert names == sorted(["ordinary", *(f"{kept} {c}" for c in member_goals.CADENCES)])


TOKEN = "ghp_" + "a" * 36


def test_goal_strings_leave_the_record_scrubbed(_floor_monkeypatch) -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, f"use {TOKEN} for releases", by="crewmate")
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"kr {TOKEN}", "target": "1"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, TOKEN[:40])
    member_goals.add_short_term(slug, ORDINARY, f"rotate {TOKEN}", by="crewmate")
    view = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))
    assert TOKEN not in json.dumps(view)
    assert "[REDACTED" in view["long_term"]


def test_the_goals_tab_saves_over_a_goal_it_was_shown_scrubbed() -> None:
    """The page sends back the scrubbed text as expected; that is not a conflict."""
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, f"use {TOKEN}", by="crewmate")
    shown = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))["long_term"]
    assert shown != f"use {TOKEN}"
    member_goals.set_long_term(slug, ORDINARY, "use the vault", by="user", expected=shown)
    member_goals.set_key_results(slug, ORDINARY, [{"text": f"kr {TOKEN}", "target": "1"}])
    shape = member_goals.public_view(member_goals.read_goals(slug, ORDINARY))["key_results"]
    member_goals.set_key_results(slug, ORDINARY, [{"text": "kr", "target": "1"}], expected=shape)
    with pytest.raises(member_goals.GoalError):
        member_goals.set_long_term(slug, ORDINARY, "x", by="user", expected="something else")


def test_a_report_is_saved_scrubbed() -> None:
    from kiro_crew import artifacts as artifacts_mod

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    saved = goal_reminder.save_report(
        slug,
        ORDINARY,
        "Scout",
        kind="weekly",
        title=f"Week {TOKEN}",
        markdown=f"token {TOKEN}",
        session_key="s",
    )
    art = artifacts_mod.get_default_store().get(saved["slug"])
    assert TOKEN not in (art.content or "") and TOKEN not in art.name


def test_wide_readings_never_lock_the_record(_floor_monkeypatch) -> None:
    """CJK readings are 3 bytes a character: the oldest go first so it still reads."""
    _seed()
    slug = _slug(ORDINARY)
    clock = [1_000_000_000.0]

    def tick() -> float:
        clock[0] += 1
        return clock[0]

    _floor_monkeypatch.setattr(member_goals, "_now", tick)
    member_goals.set_long_term(slug, ORDINARY, "目" * member_goals.LONG_TERM_MAX_CHARS, by="user")
    items = [{"text": "指" * 200, "target": "目" * 40} for _ in range(3)]
    items = [{**i, "text": f"{n}" + i["text"][1:]} for n, i in enumerate(items)]
    member_goals.set_key_results(slug, ORDINARY, items)
    ids = [k["id"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]]
    for n in range(member_goals.MAX_READINGS):
        for kid in ids:
            member_goals.record_key_result(slug, ORDINARY, kid, f"{n:03d}" + "读" * 37)
    raw = member_goals.member_goals_path(slug).read_bytes()
    assert len(raw) <= member_goals._RECORD_MAX_BYTES
    rec = member_goals.read_goals(slug, ORDINARY)
    newest = rec["key_results"][0]["readings"][-1]["value"]
    assert newest.startswith(f"{member_goals.MAX_READINGS - 1:03d}")
    assert len(rec["key_results"][0]["readings"]) < member_goals.MAX_READINGS


def test_a_key_results_card_undo_restores_ids_and_readings(tmp_path: Path) -> None:
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    one = [{"text": "PRs described", "target": "27"}]
    member_goals.set_key_results(slug, ORDINARY, one)
    original = member_goals.read_goals(slug, ORDINARY)["key_results"][0]
    member_goals.record_key_result(slug, ORDINARY, original["id"], "19")
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    new = [{"text": "notes out", "target": "yes"}]
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": new}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    assert _owner(state, "PUT", apply["path"], apply["body"])[0] == 200
    # A reading after the apply is not a conflict for the Undo.
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, "no")
    now = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert not cards.undo_snapshot_changed(catalog.KIND_CREWMATE_KEY_RESULTS, now, after)
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 200, body
    (restored,) = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert restored["id"] == original["id"]
    assert [r["value"] for r in restored["readings"]] == ["19"]
    assert restored["current"] == "19"


def test_a_key_results_cards_public_plan_carries_no_unscrubbed_reading(tmp_path: Path) -> None:
    """The plan reaches every dashboard socket (CardStore.public): readings scrubbed."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    member_goals.set_key_results(slug, ORDINARY, [{"text": "PRs described", "target": "27"}])
    kid = member_goals.read_goals(slug, ORDINARY)["key_results"][0]["id"]
    member_goals.record_key_result(slug, ORDINARY, kid, TOKEN)
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": "notes out", "target": "yes"}]},
    )
    preview = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})
    store = cards.CardStore(None)
    rec = store.propose(
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        kind=catalog.KIND_CREWMATE_KEY_RESULTS,
        params=params,
        reason="",
        preview=preview,
        before=before,
        context={},
    )
    store.begin_step(rec, revision=rec["revision"], op="apply", index=0)
    store.record_success(rec, op="apply", index=0, evidence={})
    undo, reason = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    store.complete_apply(rec, after={}, undo=undo, undo_reason=reason)
    public = json.dumps(store.public(rec))
    assert TOKEN not in public
    (step,) = store.public(rec)["plan"]["undo"]
    assert [k["id"] for k in step["body"]["restore"]] == [kid]


def test_a_key_results_card_undo_keeps_readings_recorded_since_on_a_kept_key_result(
    tmp_path: Path,
) -> None:
    """Undo puts back what the card removed; what it kept stays as it is now."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    a, b = {"text": "PRs described", "target": "27"}, {"text": "notes out", "target": "yes"}
    member_goals.set_key_results(slug, ORDINARY, [a, b])
    ids = [k["id"] for k in member_goals.read_goals(slug, ORDINARY)["key_results"]]
    member_goals.record_key_result(slug, ORDINARY, ids[0], "5")
    member_goals.record_key_result(slug, ORDINARY, ids[1], "no")
    before = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    params = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": [a]}
    )
    (apply,) = catalog.build_preview(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, {})["apply"]
    assert _owner(state, "PUT", apply["path"], apply["body"])[0] == 200
    member_goals.record_key_result(slug, ORDINARY, ids[0], "9")
    (undo,), _ = catalog.build_undo(catalog.KIND_CREWMATE_KEY_RESULTS, params, before, [{}], 1)
    status, body = _owner(state, "PUT", undo["path"], undo["body"])
    assert status == 200, body
    kept, back = member_goals.read_goals(slug, ORDINARY)["key_results"]
    assert (kept["id"], kept["current"]) == (ids[0], "9")
    assert [r["value"] for r in kept["readings"]] == ["5", "9"]
    assert (back["id"], back["current"]) == (ids[1], "no")


def test_two_concurrent_schedule_saves_both_keep_their_check_in(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    """A save that read the record first can never remove the next save's schedule.

    The first request's reconciliation is held until the second request either
    waits for the crewmate's lock or has finished its own reconciliation; without
    the lock it is the second, and the first then reconciles from its stale record.
    """
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    path = f"/api/members/{slug}/goals/schedules"
    real_sync = goal_reminder.sync_schedule
    real_lock = goal_routes._schedule_lock
    calls: list[str] = []

    async def go(c):
        first_syncing = asyncio.Event()
        second_waiting = asyncio.Event()
        second_synced = asyncio.Event()

        class Watched:
            def __init__(self, lock: asyncio.Lock) -> None:
                self.lock = lock

            async def __aenter__(self) -> None:
                if self.lock.locked():
                    second_waiting.set()
                await self.lock.acquire()

            async def __aexit__(self, *_exc: Any) -> None:
                self.lock.release()

        async def sync(*args: Any, **kwargs: Any) -> None:
            calls.append("sync")
            if len(calls) == 1:
                first_syncing.set()
                waits = [
                    asyncio.ensure_future(second_waiting.wait()),
                    asyncio.ensure_future(second_synced.wait()),
                ]
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                for w in waits:
                    w.cancel()
                await real_sync(*args, **kwargs)
                return
            await real_sync(*args, **kwargs)
            second_synced.set()

        _floor_monkeypatch.setattr(goal_reminder, "sync_schedule", sync)
        _floor_monkeypatch.setattr(goal_routes, "_schedule_lock", lambda s: Watched(real_lock(s)))

        async def put(body: dict) -> int:
            r = await c.request("PUT", path, json=body, headers={"X-Test-Auth": "owner"})
            return r.status

        first = asyncio.ensure_future(put({"member": ORDINARY, "daily": True}))
        await first_syncing.wait()
        second = asyncio.ensure_future(put({"member": ORDINARY, "weekly": True}))
        return await asyncio.gather(first, second)

    assert run_guide_app(go, state, extra_routes=OWNER_ROUTES) == [200, 200]
    jobs = _jobs_by_cadence(state, slug)
    assert (len(jobs["daily"]), len(jobs["weekly"]), jobs["monthly"]) == (1, 1, [])


def test_two_key_results_with_the_same_text_and_target_are_refused() -> None:
    _seed()
    slug = _slug(ORDINARY)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    twin = [{"text": "PRs described", "target": "27"}] * 2
    with pytest.raises(member_goals.GoalError) as exc:
        member_goals.set_key_results(slug, ORDINARY, twin)
    assert exc.value.code == "duplicate_key_result"
    # The same pair once the record's own cleaning has run is the same pair.
    spaced = [{"text": "PRs described", "target": "27"}, {"text": "PRs\tdescribed", "target": "27"}]
    with pytest.raises(member_goals.GoalError):
        member_goals.set_key_results(slug, ORDINARY, spaced)
    assert member_goals.read_goals(slug, ORDINARY)["key_results"] == []
    for items in (twin, spaced):
        with pytest.raises(catalog.CardCatalogError) as card_exc:
            catalog.validate_params(
                catalog.KIND_CREWMATE_KEY_RESULTS, {"member": ORDINARY, "items": items}
            )
        assert "same text and target" in str(card_exc.value)


def test_goal_card_params_read_as_the_record_stores_them() -> None:
    """A tab, a line break and a format character: the after-read matches the params."""
    from kiro_crew.dashboard import change_cards as cards

    _seed()
    slug = _slug(ORDINARY)
    coder = "\U0001f469\u200d\U0001f4bb"
    goal = catalog.validate_params(
        catalog.KIND_CREWMATE_GOAL,
        {"member": ORDINARY, "long_term": f"Ship\tit\nthen {coder} rest"},
    )
    member_goals.set_long_term(slug, ORDINARY, goal["long_term"], by="user")
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_GOAL)
    assert goal["long_term"] == "Ship it\nthen \U0001f469\U0001f4bb rest"
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_GOAL, goal, after, [{}])
    krs = catalog.validate_params(
        catalog.KIND_CREWMATE_KEY_RESULTS,
        {"member": ORDINARY, "items": [{"text": f"two\nlines {coder}", "target": "1\u200d0"}]},
    )
    member_goals.set_key_results(slug, ORDINARY, krs["items"])
    after = cards._goal_state(ORDINARY, catalog.KIND_CREWMATE_KEY_RESULTS)
    assert krs["items"] == [{"text": "two lines \U0001f469\U0001f4bb", "target": "10"}]
    assert cards.after_shows_the_write(catalog.KIND_CREWMATE_KEY_RESULTS, krs, after, [{}])


SHORT_DELETE = (
    "DELETE",
    "/api/members/{slug}/goals/short-term/{goal_id}",
    goal_routes.api_member_goals_short_delete,
)


def _owner_raw(state: FakeState, method: str, path: str, **kw: Any) -> tuple[int, dict]:
    async def go(c):
        r = await c.request(method, path, headers={"X-Test-Auth": "owner"}, **kw)
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[*OWNER_ROUTES, SHORT_DELETE])


def test_the_owner_adds_rewords_checks_off_and_removes_a_short_term_goal(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    base = f"/api/members/{slug}/goals/short-term"
    status, body = _owner_raw(state, "POST", base, json={"member": ORDINARY, "text": "draft"})
    assert status == 200, body
    (item,) = body["short_term"]
    assert (item["text"], item["updated_by"]) == ("draft", "user")
    path = f"{base}/{item['id']}"
    status, body = _owner_raw(
        state, "PATCH", path, json={"member": ORDINARY, "text": "draft notes", "status": "done"}
    )
    assert status == 200 and body["short_term"][0]["status"] == "done"
    assert body["last_crewmate_change"] is None
    status, body = _owner_raw(state, "PATCH", path, json={"member": ORDINARY, "status": "nope"})
    assert (status, body["code"]) == (400, "invalid_status")
    status, body = _owner_raw(state, "DELETE", f"{path}?member={ORDINARY}")
    assert status == 200 and body["short_term"] == []
    status, body = _owner_raw(state, "DELETE", f"{path}?member={ORDINARY}")
    assert (status, body["code"]) == (404, "goal_not_found")


@pytest.mark.parametrize(
    "method, suffix, kw, want",
    [
        ("PUT", "/long-term", {"data": b"{not json"}, (400, "invalid_json")),
        ("PUT", "/long-term", {"json": ["a list"]}, (400, "invalid_json")),
        ("PUT", "/long-term", {"json": {"member": ORDINARY}}, (400, "missing_long_term")),
        (
            "PUT",
            "/long-term",
            {"json": {"member": ORDINARY, "long_term": "x", "expected": 3}},
            (400, "invalid_expected"),
        ),
        ("PUT", "/long-term", {"json": {"long_term": "x"}}, (400, "missing_member")),
        (
            "PUT",
            "/long-term",
            {"json": {"member": "nobody", "long_term": "x"}},
            (404, "member_not_found"),
        ),
        ("PUT", "/key-results", {"json": {"member": ORDINARY}}, (400, "missing_items")),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [], "expected": "x"}},
            (400, "invalid_expected"),
        ),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [], "restore": "x"}},
            (400, "invalid_restore"),
        ),
        (
            "PUT",
            "/key-results",
            {"json": {"member": ORDINARY, "items": [{"text": "a", "target": "1"}] * 2}},
            (400, "duplicate_key_result"),
        ),
        (
            "PUT",
            "/schedules",
            {"json": {"member": ORDINARY, "timezone": "Mars/Base"}},
            (400, "invalid_timezone"),
        ),
        ("PUT", "/schedules", {"json": {"member": ORDINARY}}, (400, "invalid_schedule")),
        ("GET", "?member=nobody", {}, (404, "member_not_found")),
    ],
)
def test_the_owner_goal_routes_refuse_malformed_requests(
    tmp_path: Path, method: str, suffix: str, kw: dict, want: tuple[int, str]
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")
    status, body = _owner_raw(state, method, f"/api/members/{slug}/goals{suffix}", **kw)
    assert (status, body.get("code")) == want, body


def test_the_owner_goal_routes_refuse_a_bad_slug_and_an_unreadable_record(tmp_path: Path) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    status, body = _owner_raw(state, "GET", f"/api/members/BAD..slug/goals?member={ORDINARY}")
    assert (status, body["code"]) == (400, "invalid_member_slug")
    path = member_goals.member_goals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    for method, suffix, kw in (
        ("GET", f"?member={ORDINARY}", {}),
        ("PUT", "/long-term", {"json": {"member": ORDINARY, "long_term": "x"}}),
        ("PUT", "/schedules", {"json": {"member": ORDINARY, "daily": True}}),
    ):
        status, body = _owner_raw(state, method, f"/api/members/{slug}/goals{suffix}", **kw)
        assert (status, body["code"]) == (500, "goals_unreadable"), (method, suffix)


def test_a_schedule_that_cannot_be_written_turns_its_switch_back_off(
    tmp_path: Path, _floor_monkeypatch
) -> None:
    _seed()
    slug = _slug(ORDINARY)
    state = _reminder_state(tmp_path)
    member_goals.set_long_term(slug, ORDINARY, "Ship", by="user")

    async def broken(*_a: Any, **_kw: Any) -> None:
        raise OSError("cron store down")

    _floor_monkeypatch.setattr(goal_reminder, "sync_schedule", broken)
    status, body = _owner_raw(
        state,
        "PUT",
        f"/api/members/{slug}/goals/schedules",
        json={"member": ORDINARY, "daily": True},
    )
    assert (status, body["code"]) == (503, "schedule_failed")
    assert member_goals.read_goals(slug, ORDINARY)["schedules"]["daily"] is False


@pytest.mark.parametrize(
    "tool, args, path, reply, want",
    [
        ("goal_read", {}, "/api/member/self/goals/read", {"long_term": "Ship"}, '"Ship"'),
        (
            "goal_read",
            {},
            "/api/member/self/goals/read",
            {"error": "not here", "code": "not_a_crewmate"},
            "needs your own chat",
        ),
        (
            "key_result_progress",
            {"id": "k00000001", "current": "3"},
            "/api/member/self/goals/key-result",
            {"ok": True},
            "shows on your Goals tab",
        ),
        (
            "key_result_progress",
            {"id": "k00000001", "current": "3"},
            "/api/member/self/goals/key-result",
            {"error": "no key result has that id", "code": "key_result_not_found"},
            "The reading was not saved",
        ),
        (
            "save_goal_report",
            {"kind": "weekly", "title": "Week 41", "markdown": "# done"},
            "/api/member/self/goals/report",
            {"ok": True, "title": "Week 41", "url": "/artifacts/week-41"},
            "[Week 41](/artifacts/week-41)",
        ),
        (
            "save_goal_report",
            {"kind": "weekly", "title": "Week 41", "markdown": "# done"},
            "/api/member/self/goals/report",
            {"error": "the report could not be saved", "code": "report_save_failed"},
            "The report was not saved",
        ),
        (
            "short_goal_add",
            {"text": "draft"},
            "/api/member/self/goals/short-term",
            {"ok": True, "changed": True},
            "where the user can undo it",
        ),
        (
            "short_goal_update",
            {"id": "g00000001", "status": "done"},
            "/api/member/self/goals/short-term",
            {"ok": True, "changed": False},
            "nothing changed",
        ),
        (
            "short_goal_update",
            {"id": "g00000001", "text": "x"},
            "/api/member/self/goals/short-term",
            {"error": "no short-term goal has that id", "code": "goal_not_found"},
            "Your goals did not change",
        ),
        ("short_goal_update", {"id": "g00000001"}, None, {}, "needs a status or a text"),
    ],
)
def test_the_mate_goal_tools_post_their_route_and_word_the_outcome(
    _floor_monkeypatch, tool: str, args: dict, path: str | None, reply: dict, want: str
) -> None:
    sent: list[tuple[str, dict, str]] = []

    def post(p: str, body: dict, *, session_key: str) -> dict:
        sent.append((p, body, session_key))
        return reply

    _floor_monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard:m", ""))
    _floor_monkeypatch.setattr(mcp_mate, "_post", post)
    out = mcp_mate._call_tool_inner(tool, args)
    assert want in out, out
    assert [s[0] for s in sent] == ([path] if path else [])
    assert all(s[2] == "dashboard:m" for s in sent)


def test_the_mate_goal_tools_refuse_off_their_own_chat(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("", "no strict key"))
    out = mcp_mate._call_tool_inner("goal_read", {})
    assert "needs your own chat in the dashboard" in out
    assert mcp_mate._call_tool_inner("nope", {}).startswith("Error: unknown tool")
