"""Phase-role model selection for ``spawn_run(role=...)``.

``agent.role_models`` carries three phase roles beside ``background`` and
``subagent``: ``plan``, ``implement`` and ``review``. A spawn tagged with one
and carrying no model of its own runs on that role's pin; an unpinned role
falls back exactly as an untagged spawn does (the ``subagent`` pin, then the
session default). The tag selects an owner-saved pin and never names a model.

The first class is the owner gate: with nothing configured, a tagged or
untagged spawn reaches the manager with exactly the arguments it does today.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.sections import (
    ROLE_EFFORT_KEYS,
    ROLE_MODEL_KEYS,
    SPAWN_ROLE_KEYS,
    AgentConfig,
    coerce_role_efforts,
    coerce_role_models,
)
from kiro_crew.validation import (
    SPAWN_RUN_SCHEMA,
    SPAWN_RUN_TASK_ITEM_SCHEMA,
    ValidationError,
    validate_tool_args,
)

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def _config(role_models: dict[str, str] | None = None):
    """Serve *role_models* as the loaded config for the whole spawn path."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig(agent=AgentConfig(role_models=dict(role_models or {})))
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
        yield cfg


def _request(body: dict) -> tuple[Any, MagicMock]:
    """A POST /api/spawn request against a stub manager that records each spawn."""
    from kiro_crew.dashboard.state import _ChatSlot

    mgr = MagicMock()
    mgr.spawn.return_value = SimpleNamespace(id="a1", done=False, error="")
    mgr.max_concurrent = 4
    state = SimpleNamespace(
        _slots={"chat-1": _ChatSlot("chat-1", memory_mode="persistent")},
        _restricted_keys=set(),
        subagents=mgr,
        sessions=SimpleNamespace(
            get_agent=lambda key: "",
            get_agent_selection=lambda key: ("template", ""),
        ),
        conversation_log=SimpleNamespace(get_metadata_status=lambda key: ({}, True)),
    )
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": body.get("parent_session", "")}

    async def _json() -> dict:
        return body

    request.json = _json
    return request, mgr


def _api_spawn(body: dict, role_models: dict[str, str] | None = None) -> tuple[Any, dict, Any]:
    """Run the real handler once; return (response, response JSON, spawn kwargs)."""
    from kiro_crew.dashboard.handlers.messaging import api_spawn

    request, mgr = _request(body)
    with _config(role_models):
        resp = asyncio.run(api_spawn(request))
    payload = json.loads(resp.body)
    kwargs = mgr.spawn.call_args.kwargs if mgr.spawn.called else None
    return resp, payload, kwargs


class TestDefaultPathIsUnchanged:
    """Owner gate: nothing configured means every spawn runs as it does today."""

    def test_new_roles_default_to_inherit(self):
        assert AgentConfig().role_models == {}
        for role in SPAWN_ROLE_KEYS:
            assert AgentConfig().resolve_model(role) == "auto"

    def test_an_untagged_spawn_reaches_the_manager_unchanged(self):
        resp, payload, kwargs = _api_spawn({"task": "x"})
        assert resp.status == 200
        assert kwargs["model"] is None
        assert not {"role", "role_model", "role_model_source"} & set(payload)

    @pytest.mark.parametrize("role", SPAWN_ROLE_KEYS)
    def test_a_tagged_spawn_with_nothing_configured_matches_an_untagged_one(self, role):
        _, _, untagged = _api_spawn({"task": "x"})
        resp, payload, tagged = _api_spawn({"task": "x", "role": role})
        assert resp.status == 200
        assert tagged == untagged
        assert payload["role_model_source"] == "inherit"

    def test_the_tool_sends_no_role_when_none_is_named(self):
        bodies, _ = _run_tool({"tasks": ["a", "b"]}, [{"id": "a1"}, {"id": "a2"}])
        assert all("role" not in b for b in bodies)


class TestConfigKeys:
    def test_phase_roles_are_role_model_keys(self):
        assert SPAWN_ROLE_KEYS == ("plan", "implement", "review")
        assert set(SPAWN_ROLE_KEYS) <= set(ROLE_MODEL_KEYS)
        assert {"background", "subagent"} <= set(ROLE_MODEL_KEYS)

    def test_phase_roles_coerce_like_the_worker_roles(self):
        raw = {
            "plan": "model-a",
            "implement": "auto",
            "review": "  ",
            "bogus": "model-b",
            "subagent": "model-c",
        }
        assert coerce_role_models(raw) == {"plan": "model-a", "subagent": "model-c"}

    def test_non_string_values_are_dropped(self):
        assert coerce_role_models({"plan": 3, "implement": None, "review": ["x"]}) == {}

    def test_role_efforts_keep_only_the_worker_roles(self):
        assert ROLE_EFFORT_KEYS == ("background", "subagent")
        assert coerce_role_efforts({"plan": "high", "subagent": "low"}) == {"subagent": "low"}

    def test_each_phase_role_is_an_entitlement_checked_settings_key(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG, _validate_role_model

        for role in SPAWN_ROLE_KEYS:
            spec = _EDITABLE_CONFIG[f"agent.role_models.{role}"]
            assert spec["validate_fn"] is _validate_role_model
            assert spec == _EDITABLE_CONFIG["agent.role_models.subagent"]


class TestSchema:
    @pytest.mark.parametrize("role", ["", *SPAWN_ROLE_KEYS])
    def test_known_roles_are_accepted(self, role):
        assert validate_tool_args({"task": "x", "role": role}, SPAWN_RUN_SCHEMA)["role"] == role

    @pytest.mark.parametrize("bad", ["Plan", "architect", "subagent", "background", "claude-x"])
    def test_anything_else_is_refused(self, bad):
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "role": bad}, SPAWN_RUN_SCHEMA)

    def test_a_tasks_entry_takes_a_role(self):
        cleaned = validate_tool_args({"task": "x", "role": "review"}, SPAWN_RUN_TASK_ITEM_SCHEMA)
        assert cleaned["role"] == "review"
        with pytest.raises(ValidationError):
            validate_tool_args({"task": "x", "role": "boss"}, SPAWN_RUN_TASK_ITEM_SCHEMA)

    def test_the_tool_advertises_the_same_closed_set(self):
        from kiro_crew.mcp_tools.spawn import schemas

        spawn_run = next(s for s in schemas() if s["name"] == "spawn_run")
        props = spawn_run["inputSchema"]["properties"]
        assert props["role"]["enum"] == list(SPAWN_ROLE_KEYS)
        assert props["tasks"]["items"]["properties"]["role"]["enum"] == list(SPAWN_ROLE_KEYS)


class TestSelection:
    def _select(self, role: str, model: str = "", role_models: dict | None = None):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.subagent import select_spawn_role_model

        cfg = KiroCrewConfig(agent=AgentConfig(role_models=dict(role_models or {})))
        return select_spawn_role_model(role, model, cfg)

    def test_the_role_pin_is_selected(self):
        sel = self._select("implement", role_models={"implement": "m-impl", "subagent": "m-sub"})
        assert (sel.model, sel.shown, sel.source) == ("m-impl", "m-impl", "role")

    def test_an_explicit_model_wins(self):
        sel = self._select("implement", "m-own", {"implement": "m-impl"})
        assert (sel.model, sel.shown, sel.source) == ("m-own", "m-own", "spawn")

    def test_an_unpinned_role_names_the_subagent_pin_without_passing_it(self):
        sel = self._select("review", role_models={"subagent": "m-sub"})
        assert (sel.model, sel.shown, sel.source) == ("", "m-sub", "subagent")

    def test_an_auto_role_pin_inherits(self):
        sel = self._select("plan", role_models={"plan": "auto"})
        assert (sel.model, sel.source) == ("", "inherit")

    def test_a_worker_role_name_selects_nothing(self):
        sel = self._select("background", role_models={"background": "m-bg"})
        assert (sel.model, sel.source) == ("", "inherit")

    def test_an_unreadable_config_selects_nothing(self):
        from kiro_crew.subagent import select_spawn_role_model

        with patch(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            classmethod(lambda c: (_ for _ in ()).throw(OSError("unreadable"))),
        ):
            sel = select_spawn_role_model("implement", "")
        assert (sel.model, sel.source) == ("", "inherit")


class TestEndpoint:
    def test_a_role_pin_reaches_the_manager_as_the_model(self):
        resp, payload, kwargs = _api_spawn(
            {"task": "x", "role": "implement"}, {"implement": "m-impl", "subagent": "m-sub"}
        )
        assert resp.status == 200
        assert kwargs["model"] == "m-impl"
        assert payload["role"] == "implement"
        assert payload["role_model"] == "m-impl"
        assert payload["role_model_source"] == "role"

    def test_an_explicit_model_wins_over_the_role_pin(self):
        _, payload, kwargs = _api_spawn(
            {"task": "x", "role": "review", "model": "m-own"}, {"review": "m-rev"}
        )
        assert kwargs["model"] == "m-own"
        assert payload["role_model_source"] == "spawn"

    def test_an_unpinned_role_keeps_the_subagent_fallback_at_start(self):
        _, payload, kwargs = _api_spawn({"task": "x", "role": "review"}, {"subagent": "m-sub"})
        assert kwargs["model"] is None
        assert payload["role_model"] == "m-sub"
        assert payload["role_model_source"] == "subagent"

    def test_an_unknown_role_is_refused_before_any_spawn(self):
        resp, payload, kwargs = _api_spawn({"task": "x", "role": "architect"}, {"plan": "m"})
        assert resp.status == 400
        assert kwargs is None

    def test_the_role_pin_takes_the_backend_id_check(self, monkeypatch):
        """A selected pin is checked like an explicit model: kiro refuses a
        provider-qualified id whichever way it arrived."""
        from kiro_crew import acp_backends
        from kiro_crew.agent_sdk import backend_install
        from kiro_crew.agent_sdk.backend_install import INSTALLED, BackendInstallState

        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: [""])
        monkeypatch.setattr(
            backend_install,
            "probe_backend",
            lambda b: BackendInstallState(
                b,
                b or "kiro",
                INSTALLED,
                missing_components=(),
                install_command="",
                restart_required=False,
            ),
        )
        resp, payload, kwargs = _api_spawn(
            {"task": "x", "role": "implement", "backend": "kiro"},
            {"implement": "provider/model"},
        )
        assert resp.status == 400
        assert payload["code"] == "model_unavailable"
        assert kwargs is None


def _run_tool(args: dict[str, Any], responses: list[dict]) -> tuple[list[dict], str]:
    """Run spawn_run against a gateway answering from *responses*."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []
    answers = iter(responses)

    def _fake_post(path: str, body: dict, **_: Any) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            return next(answers)
        return {}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool("spawn_run", args)
    return bodies, result


class TestToolReceipt:
    def test_the_role_is_batch_wide_and_a_task_entry_overrides_it(self):
        bodies, _ = _run_tool(
            {"tasks": ["a", {"task": "b", "role": "review"}], "role": "implement"},
            [{"id": "a1"}, {"id": "a2"}],
        )
        assert [b["role"] for b in bodies] == ["implement", "review"]

    def test_each_role_line_names_its_model_and_source(self):
        _, result = _run_tool(
            {"tasks": ["a", "b", {"task": "c", "role": "review"}], "role": "implement"},
            [
                {
                    "id": "a1",
                    "role": "implement",
                    "role_model": "m-impl",
                    "role_model_source": "role",
                },
                {
                    "id": "a2",
                    "role": "implement",
                    "role_model": "m-impl",
                    "role_model_source": "role",
                },
                {"id": "a3", "role": "review", "role_model": "", "role_model_source": "inherit"},
            ],
        )
        assert (
            "ℹ role 'implement' for a1, a2: model 'm-impl' (agent.role_models.implement)" in result
        )
        assert (
            "ℹ role 'review' for a3: the session default model "
            "(agent.role_models.review and agent.role_models.subagent are unset)"
        ) in result
        assert "Spawned 3 subagent(s)" in result

    def test_a_subagent_fallback_and_an_own_model_are_named(self):
        _, result = _run_tool(
            {"tasks": ["a", {"task": "b", "model": "m-own"}], "role": "plan"},
            [
                {
                    "id": "a1",
                    "role": "plan",
                    "role_model": "m-sub",
                    "role_model_source": "subagent",
                },
                {"id": "a2", "role": "plan", "role_model": "m-own", "role_model_source": "spawn"},
            ],
        )
        assert (
            "ℹ role 'plan' for a1: model 'm-sub' "
            "(agent.role_models.subagent; agent.role_models.plan is unset)"
        ) in result
        assert "ℹ role 'plan' for a2: model 'm-own' (this call's own model)" in result

    def test_an_untagged_wave_has_no_role_line(self):
        _, result = _run_tool({"tasks": ["a", "b"]}, [{"id": "a1"}, {"id": "a2"}])
        assert "ℹ role" not in result


class TestOrchestratorEndToEnd:
    """The orchestrator's two delegated phases through the real tool and the
    real endpoint, against a stub manager: implementation and review run on
    their role models, and the planning turn itself spawns nothing."""

    def _drive(
        self, calls: list[dict], role_models: dict[str, str]
    ) -> tuple[list[dict], list[str]]:
        from kiro_crew import mcp_core
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        spawned: list[dict] = []
        counter = iter(range(1, 100))

        def _gateway(path: str, body: dict, **_: Any) -> dict:
            if path != "/api/spawn":
                return {}
            request, mgr = _request(body)
            mgr.spawn.return_value = SimpleNamespace(id=f"sa{next(counter)}", done=False, error="")
            resp = asyncio.run(api_spawn(request))
            spawned.append(dict(mgr.spawn.call_args.kwargs, task=body["task"]))
            return json.loads(resp.body)

        receipts: list[str] = []
        with (
            _config(role_models),
            patch.object(mcp_core, "_post", side_effect=_gateway),
            patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
            patch.object(mcp_core, "sel", MagicMock()),
        ):
            for call in calls:
                receipts.append(mcp_core._call_tool("spawn_run", call))
        return spawned, receipts

    def test_phases_run_on_their_role_models(self):
        spawned, receipts = self._drive(
            [
                {"tasks": ["implement part 1", "implement part 2"], "role": "implement"},
                {"task": "review parts 1 and 2", "role": "review"},
            ],
            {"implement": "m-impl", "review": "m-rev", "plan": "m-plan"},
        )
        assert [s["model"] for s in spawned] == ["m-impl", "m-impl", "m-rev"]
        assert "ℹ role 'implement' for sa1, sa2: model 'm-impl'" in receipts[0]
        assert "ℹ role 'review' for sa3: model 'm-rev'" in receipts[1]

    def test_unconfigured_phases_run_where_untagged_work_runs(self):
        spawned, receipts = self._drive(
            [
                {"task": "implement", "role": "implement"},
                {"task": "review", "role": "review"},
                {"task": "plain"},
            ],
            {},
        )
        assert [s["model"] for s in spawned] == [None, None, None]
        assert "the session default model" in receipts[0]
        assert "ℹ role" not in receipts[2]


class TestOrchestratorSkill:
    """The shipped orchestrator is an opt-in skill: no triggers, phases declared
    through tagged spawns, and each phase's model reported."""

    SKILL = ROOT / "src" / "kiro_crew" / "builtin_skills" / "orchestrator" / "SKILL.md"

    def _parts(self) -> tuple[dict, str]:
        from kiro_crew.skills import SkillsLoader

        text = self.SKILL.read_text(encoding="utf-8")
        return SkillsLoader._parse_frontmatter_text(text), text

    def test_it_is_never_loaded_by_a_word_match(self):
        meta, _ = self._parts()
        assert meta.get("name") == "orchestrator"
        assert not meta.get("triggers")
        assert str(meta.get("always", "false")).lower() == "false"

    def test_it_declares_each_delegated_phase_with_a_role_tag(self):
        _, text = self._parts()
        assert 'role: "implement"' in text
        assert 'role: "review"' in text
        assert "agent.role_models" in text

    def test_it_reports_each_phase_model(self):
        _, text = self._parts()
        assert "ℹ role" in text
        assert "## Phase models" in text
