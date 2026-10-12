"""Tests for ``dashboard_save`` -- the call that STAGES a composed page.

The property guarded hardest here is the gate: a save stores NOTHING. It stages
the composed page beside the crewmate's record and hands back a link, and
``dashboard_apply`` is the only call that mints or updates the artifact. So every
storage test in this file goes through both calls, and the pair of tests at the
top prove the halves are really separate -- a save leaves the store untouched, and
an apply with nothing staged writes nothing either.

The second property is the one the binding carries: the artifact's ``bound_to``
decides whose Dashboard tab renders this page, so it is derived from the vetted
caller on the server and never read out of the body -- a caller that sends one is
refused, a staged page bound elsewhere is refused at the apply rather than
re-stamped, and a page already bound to another crewmate is never written over.

The third is that nothing here reimplements the store's own write gate. The save
runs ``canonical_package_content`` -- the same function ``ArtifactStore.create``
and ``.update`` run -- so the package's size, surrogate and closed-catalog guards
answer while the agent is still composing rather than after a person has approved
the page. The refusal tests below prove that by sending packages each guard
refuses and asserting the save answers 400 with nothing staged.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND, parse_package
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio

CREW = "fleet-crew"
SLUG = "fleet-crew"
OTHER_SLUG = "other-crew"


def _package_body() -> dict[str, Any]:
    """The three declarations a save takes. No ``kind``, and no ``bound_to``."""
    return {
        "model": {
            "types": {
                "open_prs": {
                    "type": "number",
                    "source": {"agentic": True},
                    "label": "Open PRs",
                },
                "headline": {"type": "text", "source": {"agentic": True}},
            }
        },
        "view": {
            "blocks": [
                {"id": "lead", "type": "stat", "fields": ["open_prs"], "title": "Open"},
                {"id": "note", "type": "list", "fields": ["headline"]},
            ]
        },
        "theme": {"tokens": {"--accent": "oklch(0.7 0.15 150)"}},
    }


class _Store:
    """A stand-in for ``ArtifactStore`` that keeps the real write gate.

    The gate is the point of the test, so this does NOT fake it: ``create`` and
    ``update`` call ``parse_package`` exactly where the real store calls
    ``canonical_package_content``, and raise the same
    ``ArtifactValidationError``. What is faked is only the filesystem -- the
    records live in a dict -- so a route test needs no artifact home.
    """

    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.updates: list[dict[str, Any]] = []
        #: Slugs whose ``get`` fails. A record that cannot be OPENED is the case
        #: the binding lookup must not answer "absent" for, because it may be the
        #: one bound here -- that is exactly what could not be read.
        self.unreadable: set[str] = set()

    # -- the surface the handler uses -------------------------------------- #

    def list(self, *, kind: str | None = None, **_kw: Any) -> list[SimpleNamespace]:
        return [r for r in self.records.values() if kind is None or r.kind == kind]

    def get(self, slug: str, **_kw: Any) -> SimpleNamespace:
        from kiro_crew.artifact_store.model import ArtifactNotFoundError

        if slug in self.unreadable:
            from kiro_crew.artifact_store.model import ArtifactError

            raise ArtifactError(f"{slug} cannot be opened")
        if slug not in self.records:
            raise ArtifactNotFoundError(slug)
        return self.records[slug]

    def create(self, *, name: str, content: str, kind: str | None = None, **_kw: Any):
        from kiro_crew.artifact_store.dashboard_package import canonical_package_content

        if kind == DASHBOARD_KIND:
            content = canonical_package_content(content)
        slug = f"dashboard-{len(self.records)}"
        rec = SimpleNamespace(
            slug=slug, name=name, content=content, kind=kind or "markdown", version=1
        )
        self.records[slug] = rec
        return rec

    def update(self, slug: str, *, content: str | None = None, **kw: Any):
        from kiro_crew.artifact_store.dashboard_package import (
            canonical_package_content,
            layout_changed,
        )

        rec = self.get(slug)
        self.updates.append({"slug": slug, **kw})
        if content is not None:
            new = canonical_package_content(content)
            if layout_changed(rec.content, new):
                rec.version += 1
            rec.content = new
        return rec


@pytest.fixture
def store(monkeypatch) -> _Store:
    s = _Store()
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: s, raising=False)
    return s


@asynccontextmanager
async def _client(monkeypatch, *, resolved: tuple[str, str, str] | None = (SLUG, CREW, "slot-1")):
    """The panel routes with the caller already vetted, or already refused.

    ``resolved=None`` is the shape every caller the shared gate turns away has --
    a subagent with no dashboard slot, a cookie-only caller, an app identity, a
    restricted session. Those refusals are ``_resolve_dashboard_caller``'s and are
    proven on its own tests; what this file has to prove is that a save ASKS it
    and writes nothing when it says no.
    """

    async def _resolve(_request, _operation):
        if resolved is None:
            return None, web.json_response(
                {"error": "not a dashboard thread", "code": "no_dashboard_slot"}, status=400
            )
        return resolved, None

    monkeypatch.setattr(routes, "_resolve_dashboard_caller", _resolve)
    app = web.Application()
    app["state"] = SimpleNamespace(broadcast_ws=lambda *_a, **_k: None)
    routes.register_agent_panel_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def _save(client: TestClient, body: dict[str, Any]):
    return await client.post("/api/agent-panel/dashboard/save", json=body)


async def _apply(client: TestClient):
    """The person's "yes". Takes no body, like the route."""
    return await client.post("/api/agent-panel/dashboard/apply", json={})


# --------------------------------------------------------------------------- #
# 0. THE GATE: a save stages, an apply installs
# --------------------------------------------------------------------------- #


async def test_a_save_stages_the_page_and_stores_nothing(monkeypatch, store):
    """The whole gate in one test: nothing is written and nothing is live.

    The store is untouched -- not an empty page, not a version 0 record, nothing
    at all -- and the response says ``staged`` rather than ``saved``, carrying the
    link a person opens. An agent that read ``saved`` here would tell them their
    dashboard had changed when the next thing it has to do is ask them.
    """
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert "saved" not in payload, "a save reported a stored page"
    staged = payload["staged"]
    assert staged["fields"] == 2
    assert staged["blocks"] == 2
    assert staged["preview_url"] == f"/members?member={SLUG}&dashboard=preview"
    # The one instruction, travelling with the thing it is about.
    assert "dashboard_apply" in payload["next"]
    # NOTHING STORED. This is the assertion the whole ruling rests on.
    assert store.records == {}
    assert store.updates == []


async def test_the_apply_of_a_package_is_refused_until_a_renderer_lands(monkeypatch, store):
    """Option 1 (buluoray review, 2026-10-12): apply-of-package is refused on this base.

    Applying would bind the package, and a bound package freezes the crewmate's live
    page -- `api_member_dashboard` keeps drawing the template while every agentic
    `dashboard_write` is refused with `package_display_pending`, with no agent verb
    back. So while nothing draws a package, the apply is refused: the 409
    `package_display_pending`, and NOTHING is written. #19335 is the renderer that
    lifts this. The staged page is kept, so a save followed by this refusal still
    leaves the page there to look at.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        assert store.records == {}, "the save wrote before anybody was asked"
        resp = await _apply(client)
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "package_display_pending"

    # NOTHING BOUND: no artifact created or updated, so no page is frozen.
    assert store.records == {}
    assert store.updates == []
    # The staged page is left in place -- the refusal was a read, not a discard.
    assert instance_store.staged_package_preview(SLUG) is not None


async def test_a_refused_package_apply_leaves_template_writes_working(monkeypatch, store):
    """The point of refusing (buluoray's asked-for assertion): nothing is frozen.

    buluoray asked for a test that applies a package and then asserts what a
    template-field `dashboard_write` returns. Because the apply is refused and
    nothing is bound, `read_package_model` still answers from the template instance
    (`from_package=False`) -- so `dashboard_write` keeps checking against the
    template and keeps working, instead of being refused `package_display_pending`.
    """
    from kiro_crew import dashboard_package as reader

    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        assert (await _apply(client)).status == 409

    # No package is bound, so the write path still reads the template, not a package.
    model = reader.read_package_model(SLUG)
    assert model.from_package is False, "a refused apply still froze the live page"


async def test_an_apply_with_nothing_staged_writes_nothing(monkeypatch, store):
    """No save, no page. The template path answers, and it has nothing either."""
    async with _client(monkeypatch) as client:
        resp = await _apply(client)
        assert resp.status == 400, await resp.text()

    assert store.records == {}
    assert store.updates == []


async def test_a_second_apply_of_a_staged_package_is_refused_the_same_way(monkeypatch, store):
    """The refusal is a plain read, so it does not consume the staged page.

    A second apply finds the same staged package and answers the same 409 -- the
    staged page is only cleared by a save that replaces it or a discard, never by
    this refusal.
    """
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        assert (await _apply(client)).status == 409
        second = await _apply(client)
        assert second.status == 409, await second.text()
        assert (await second.json())["code"] == "package_display_pending"

    assert store.records == {}
    assert store.updates == []


async def test_a_staged_page_bound_elsewhere_is_also_refused_at_the_apply(monkeypatch, store):
    """A package staged under another binding is refused too, and nothing is bound.

    On this base the refusal is the same `package_display_pending` -- the apply does
    not reach the binding check because it does not commit at all -- so a mismatched
    staged file, like any staged package, installs nothing. Reached by staging a
    package the save route would never produce, which is what an older staging file
    on disk looks like.
    """
    from kiro_crew.artifact_store.dashboard_package import canonical_package_content
    from kiro_crew.dashboard_templates import instance as instance_store

    theirs = _package_body()
    theirs["kind"] = DASHBOARD_KIND
    theirs["bound_to"] = f"crewmate:{OTHER_SLUG}"
    instance_store.stage_package_preview(
        SLUG, content=canonical_package_content(json.dumps(theirs))
    )

    async with _client(monkeypatch) as client:
        resp = await _apply(client)
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "package_display_pending"

    assert store.records == {}


# --------------------------------------------------------------------------- #
# 1. An applied page round-trips
# --------------------------------------------------------------------------- #


async def test_the_binding_comes_from_the_caller_and_the_body_cannot_carry_one(monkeypatch, store):
    """``bound_to`` in the body is refused, not merged and not ignored.

    Ignoring it would be safe and still wrong: an agent that sent one would be
    told its save succeeded while the page went somewhere else, and would send it
    again every cycle. The refusal is the only answer it can act on.
    """
    body = _package_body()
    body["bound_to"] = f"crewmate:{OTHER_SLUG}"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert "bound_to" in payload["error"]
    assert store.records == {}


# --------------------------------------------------------------------------- #
# 2. An unchanged layout cuts no version
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 3. An invalid package is refused with a 400-class error
# --------------------------------------------------------------------------- #


async def test_an_unknown_block_type_is_refused(monkeypatch, store):
    body = _package_body()
    body["view"]["blocks"][0]["type"] = "hologram"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert "hologram" in payload["error"]
    assert store.records == {}


async def test_an_oversized_package_is_refused(monkeypatch, store):
    """Over the package cap, refused as a package and not as a 500."""
    body = _package_body()
    body["model"]["types"]["open_prs"]["description"] = "x" * 400_000
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert payload["code"] == "invalid_package"
    assert store.records == {}


def _lone_surrogate_body() -> bytes:
    """A package whose block title is an unpaired ``\\ud800``, as raw JSON bytes.

    Raw rather than through ``json=``: the escape has to reach the handler AS
    JSON, because it is six ASCII characters until the decoder turns it into the
    single code point no UTF-8 encoder accepts.
    """
    body = _package_body()
    body["view"]["blocks"][0]["title"] = "LONE"
    return json.dumps(body).replace("LONE", "\\ud800").encode("utf-8")


async def test_a_lone_surrogate_is_refused(monkeypatch, store):
    async with _client(monkeypatch) as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/save",
            data=_lone_surrogate_body(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    assert store.records == {}


async def test_a_lone_surrogate_never_reaches_the_real_store_either(monkeypatch, tmp_path):
    """The same body with a REAL store behind the route, which it must never reach.

    The refusal is the save's now, because the save runs the dashboard gate itself
    and nothing is handed to a store until an apply. So the thing worth pinning
    with a real store wired up is that the store stays EMPTY: no record, and no
    apply to make one, because the package was refused while the agent was still
    composing. The store's own refusal, for every other caller, is pinned in
    ``test_artifact_update_refuses_a_surrogate_the_same_way``.
    """
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real, raising=False)
    async with _client(monkeypatch) as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/save",
            data=_lone_surrogate_body(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_package"
        # And nothing was staged for an apply to pick up.
        assert (await _apply(client)).status == 400
    assert real.list(kind=DASHBOARD_KIND) == []


async def test_artifact_update_refuses_a_surrogate_the_same_way(tmp_path):
    """The store's OWN refusal, on ``update`` as well as ``create``.

    The clean refusal belongs to the store, so it holds for every caller --
    including a plain ``artifact_update`` the save route never touches.
    ``_validate_content`` is the first call in both methods and it measures
    content by encoding it, and content holding an unpaired surrogate cannot be
    encoded at all: answering that with ``UnicodeEncodeError`` would turn an
    artifact a caller can fix into a 500.
    """
    from kiro_crew.artifact_store.model import ArtifactValidationError
    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "artifacts")
    pkg = _package_body()
    pkg["kind"] = DASHBOARD_KIND
    pkg["bound_to"] = f"crewmate:{SLUG}"
    created = store.create(
        name="page", content=json.dumps(pkg), kind=DASHBOARD_KIND, source="dashboard"
    )

    # A REAL lone code point in the content text, which is what reaches the store:
    # the escape ``\\ud800`` is six ASCII characters and encodes fine, so a package
    # built by replacing text with it would exercise the decoder's refusal instead
    # and pass whatever ``_validate_content`` does. ``ensure_ascii=False`` is what
    # keeps the code point itself in the output, and is what the save route uses.
    surrogate = dict(pkg)
    surrogate["view"] = {"blocks": [dict(pkg["view"]["blocks"][0], title="\ud800")]}
    bad = json.dumps(surrogate, ensure_ascii=False)
    assert "\ud800" in bad
    with pytest.raises(ArtifactValidationError) as update_refusal:
        store.update(created.slug, content=bad)
    assert "surrogate" in str(update_refusal.value)

    with pytest.raises(ArtifactValidationError) as create_refusal:
        store.create(name="other", content=bad, kind=DASHBOARD_KIND, source="dashboard")
    assert "surrogate" in str(create_refusal.value)

    # The live package is untouched by the refused update, and the control: the
    # same bytes without the surrogate are still accepted, so the refusal above
    # is the surrogate's and not the whole package being rejected.
    assert parse_package(store.get(created.slug).content)["bound_to"] == f"crewmate:{SLUG}"
    assert store.update(created.slug, content=json.dumps(pkg)) is not None


async def test_a_credential_shaped_field_name_is_refused_at_save(monkeypatch, store):
    """A name a redactor would mask is refused HERE, and nothing is written.

    The field-name grammar both sides enforce, ``[a-z][a-z0-9_]{0,63}``, admits a
    credential shape: ``ghp_`` plus 36 lowercase characters satisfies it. The page
    refuses a read whose declared names a redactor would rewrite, and it refuses
    the WHOLE read, because a field name is the key joining the read's fields, the
    frame's blocks and the page's cells. So a save that stored such a name would
    succeed and hand the crewmate a page that can never render -- while this
    route's reply says the tab draws it. That reply would be false, so the save
    refuses instead.

    The premise is asserted on the REDACTOR rather than on a hand-written pattern,
    because the redactor is what both sides consult. A test that invented its own
    pattern would pin a rule nothing enforces.

    The refusal names a POSITION, not the name. The page's own counter withholds
    the offending name on purpose, and the MCP boundary redacts this sentence
    anyway, so a name would reach the agent masked while travelling raw through the
    gateway's logs. A position resolves against the package in the agent's own hand.
    """
    from kiro_crew.platform import redact_via_context

    token_name = "ghp_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    # The two halves of the premise, each asserted rather than assumed.
    assert redact_via_context(token_name) != token_name, "not credential-shaped here"
    assert redact_via_context("open_prs") == "open_prs", "control name is not redacted"

    body = _package_body()
    body["model"]["types"][token_name] = {"type": "number", "source": {"agentic": True}}
    body["view"]["blocks"][0]["fields"] = [token_name]

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    assert "looks like a credential" in payload["error"]
    # A position the agent can resolve, and NOT the name itself.
    assert "declared field #" in payload["error"]
    assert token_name not in payload["error"], "the refusal echoed the name back"
    # NOTHING WRITTEN. A refusal that minted the artifact first would be a hole the
    # status code hides, which is the assertion every other refusal here makes.
    assert store.records == {}
    assert store.updates == []


async def test_an_ordinary_field_name_still_saves(monkeypatch, store):
    """The control for the refusal above, and why it is not a blanket block.

    Without it, a route that refused EVERY package would pass that test. The body
    is this file's ordinary one, whose names no redactor touches. A save stages and
    stores nothing, so the control is that the page STAGES and the store stays empty.
    """
    from kiro_crew.dashboard_templates import instance as instance_store

    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()

    assert instance_store.staged_package_preview(SLUG) is not None
    assert store.records == {}


async def test_a_credential_shaped_block_id_is_refused_too(monkeypatch, store):
    """The other name set the page counts: a view block's id.

    Both reach a page as keys -- a field name through the read and the manifest, a
    block id through the frame's ``blocks`` mapping -- so a check catching only one
    would leave the same hole one key away.
    """
    from kiro_crew.platform import redact_via_context

    token_id = "ghp_" + "f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1"
    assert redact_via_context(token_id) != token_id, "not credential-shaped here"

    body = _package_body()
    body["view"]["blocks"][0]["id"] = token_id

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    assert "view block #" in payload["error"]
    assert token_id not in payload["error"]
    assert store.records == {}


async def test_a_credential_shaped_theme_token_is_refused_too(monkeypatch, store):
    """The THIRD name set a package carries, and the one whose grammar hides it.

    A field name and a block id match ``[a-z][a-z0-9_]{0,63}``. A theme token is a
    CSS custom property, ``--[a-z][a-z0-9-]{0,63}``, so a lowercase HYPHENATED
    look-alike satisfies it where an underscored one never could -- which is how a
    check written for the first two name sets leaves this one open.

    Refused rather than redacted on the way out, for the reason the other two are:
    a token key is a JOIN KEY. The page's own style references the variable by
    name, so a redactor rewriting it hands back a stylesheet pointing at a property
    that does not exist. There is no masked form that still works.
    """
    from kiro_crew.platform import redact_via_context

    token_key = "--xoxb-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    # The two halves of the premise, each asserted rather than assumed.
    assert redact_via_context(token_key) != token_key, "not credential-shaped here"
    assert redact_via_context("--accent") == "--accent", "control token is not redacted"

    body = _package_body()
    body["theme"]["tokens"][token_key] = "oklch(0.5 0.1 200)"

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    # A position the agent can resolve, and NOT the name itself.
    assert "theme token #" in payload["error"]
    assert token_key not in payload["error"], "the refusal echoed the name back"
    # NOTHING STAGED and nothing stored, so no apply can reach it either.
    assert store.records == {}
    async with _client(monkeypatch) as client:
        assert (await _apply(client)).status == 400


async def test_an_ordinary_theme_token_still_stages(monkeypatch, store):
    """The control: without it, a route refusing EVERY theme would pass above."""
    from kiro_crew.dashboard_templates import instance as instance_store

    body = _package_body()
    body["theme"]["tokens"]["--surface"] = "oklch(0.98 0.01 250)"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 200, await resp.text()

    assert instance_store.staged_package_preview(SLUG) is not None
    assert store.records == {}


async def test_the_real_store_round_trips_a_valid_package(monkeypatch, tmp_path):
    """The positive control for the surrogate test, through the real store.

    Without it, a route that refused EVERY package would pass the surrogate test,
    so this is what proves the 400 there came from the surrogate and not from the
    save path being broken against a real store. A save stages and stores nothing,
    so what this pins is that a valid package STAGES against a real store and that
    the store stays empty until an apply (refused on this base).
    """
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard_templates import instance as instance_store

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real, raising=False)
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        assert "staged" in await resp.json()

    assert instance_store.staged_package_preview(SLUG) is not None
    assert real.list(kind=DASHBOARD_KIND) == []


# --------------------------------------------------------------------------- #
# 4. A caller the shared gate turns away saves nothing
# --------------------------------------------------------------------------- #


async def test_a_caller_with_no_dashboard_slot_is_refused(monkeypatch, store):
    """The subagent case. A subagent holds no slot, so the shared gate refuses it.

    Asserting the STORE stayed empty is the part that matters: a save that
    answered 400 after minting the artifact would be a hole the status code hides.
    """
    async with _client(monkeypatch, resolved=None) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_dashboard_slot"
    assert store.records == {}


# --------------------------------------------------------------------------- #
# 5. A caller cannot save into another crewmate's panel
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The MCP tool half
# --------------------------------------------------------------------------- #


def test_the_tool_is_on_the_panel_server_and_takes_no_target():
    from kiro_crew import mcp_panel

    tool = next(t for t in mcp_panel._tool_definitions() if t["name"] == "dashboard_save")
    props = tool["inputSchema"]["properties"]
    assert set(props) == {"model", "view", "theme"}
    assert sorted(tool["inputSchema"]["required"]) == ["model", "theme", "view"]
    assert not (set(props) & {"bound_to", "session", "session_key", "slot", "target", "kind"})


def test_the_tool_refuses_a_caller_whose_identity_is_not_strict(monkeypatch):
    """A subagent, answered by the tool before any request leaves the process."""
    from kiro_crew import mcp_panel

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("", "Error: no identity"))

    def _boom(*_a: Any, **_k: Any):  # pragma: no cover - must not run
        raise AssertionError("a caller with no strict identity reached the gateway")

    monkeypatch.setattr(mcp_panel, "_post", _boom)
    out = mcp_panel._call_tool_inner("dashboard_save", _package_body())
    assert out == "Error: no identity"


def test_the_tool_sends_only_the_three_declarations(monkeypatch):
    from kiro_crew import mcp_panel

    sent: dict[str, Any] = {}
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))

    def _post(path: str, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent["path"] = path
        sent["payload"] = payload
        return {
            "staged": {
                "fields": 2,
                "blocks": 2,
                "preview_url": "/members?member=fleet-crew&dashboard=preview",
            },
            "next": "show this preview_url to the person, ask, then call dashboard_apply",
        }

    monkeypatch.setattr(mcp_panel, "_post", _post)
    body = _package_body()
    body["bound_to"] = "crewmate:someone-else"
    out = mcp_panel._call_tool_inner("dashboard_save", body)
    assert sent["path"] == "/api/agent-panel/dashboard/save"
    assert set(sent["payload"]) == {"model", "view", "theme"}
    # STAGED, and the link to show. An agent told "saved" here would report a
    # change nobody approved, which is the one thing this reply must not say.
    assert "STAGED, not live" in out
    assert "/members?member=fleet-crew&dashboard=preview" in out
    assert "dashboard_apply" in out


def test_the_tool_hands_back_a_refusal_whole(monkeypatch):
    """The refusal names the key and the catalog, which is what the agent fixes from."""
    from kiro_crew import mcp_panel

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda *_a, **_k: {
            "error": "dashboard package: view.blocks[0].type: unknown block type 'hologram'",
            "code": "invalid_package",
        },
    )
    out = mcp_panel._call_tool_inner("dashboard_save", _package_body())
    assert "hologram" in out
    assert "view.blocks[0].type" in out


# --------------------------------------------------------------------------- #
# 6. The grant equals the registration
# --------------------------------------------------------------------------- #


def test_the_member_grant_names_the_save_tool():
    from kiro_crew.agent import _MEMBER_PANEL_GRANTS

    assert "@kirocrew-panel/dashboard_save" in _MEMBER_PANEL_GRANTS


def test_the_tool_has_a_registered_argument_schema():
    from kiro_crew.validation import MCP_PANEL_SCHEMAS

    assert "dashboard_save" in MCP_PANEL_SCHEMAS
