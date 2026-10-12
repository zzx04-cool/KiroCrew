"""A fresh first turn makes its history debt durable before the provider sees it.

Arming ``first_turn_history_owed`` also writes the sid's on-disk unlanded marker,
but on the event loop that write is only scheduled. The turn awaits the session
map's durability point (``SessionManager.aflush``) right after arming, so a crash
between the arm and the prompt cannot leave the sid on disk without its marker.
"""

from __future__ import annotations

from typing import Any

import pytest
from turn_harness import TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _order_recorder(order: list[str], *, armed: bool) -> Any:
    def _setup(ctx: TurnContext) -> None:
        sessions = ctx.state.sessions

        def _mark(*_args: Any, **_kwargs: Any) -> bool:
            order.append("mark")
            return armed

        async def _aflush(*_args: Any, **_kwargs: Any) -> None:
            prompted = ctx.provider is not None and bool(ctx.provider.recorded("stream"))
            order.append("aflush after prompt" if prompted else "aflush")

        sessions.mark_first_turn_history_owed.side_effect = _mark
        sessions.aflush.side_effect = _aflush

    return _setup


@pytest.mark.asyncio
async def test_an_armed_debt_is_flushed_before_the_prompt():
    order: list[str] = []
    record = await run_turn(
        TurnScript(events=[_REPLY, _DONE], setup=_order_recorder(order, armed=True))
    )
    assert record.provider_calls, "the turn reached the provider"
    assert order[:2] == ["mark", "aflush"]


@pytest.mark.asyncio
async def test_no_flush_when_nothing_was_armed():
    order: list[str] = []
    await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_order_recorder(order, armed=False)))
    assert order == ["mark"]
