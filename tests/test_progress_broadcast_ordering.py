"""Progress broadcasts must not finish behind a newer display transition."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.web.managers import campaigns
from src.web.managers.broadcaster import WebSocketBroadcaster
from src.web.managers.campaigns import CampaignProgressManager
from tests.test_claimed_progress_display import reward


@pytest_asyncio.fixture(params=[False, True], ids=["direct", "broadcaster"])
async def display(monkeypatch, request):
    before = asyncio.all_tasks()
    tasks = []

    def schedule(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(campaigns, "asyncio", SimpleNamespace(Lock=asyncio.Lock, create_task=schedule))
    emit = AsyncMock()
    broadcaster = WebSocketBroadcaster() if request.param else SimpleNamespace(emit=emit)
    if request.param:
        broadcaster.set_socketio(SimpleNamespace(emit=emit))
    state = SimpleNamespace(manager=CampaignProgressManager(broadcaster), emit=emit, tasks=tasks)
    try:
        yield state
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not {task for task in asyncio.all_tasks() - before
                    if task is not asyncio.current_task() and not task.done()}


async def drain(state):
    return await asyncio.wait_for(asyncio.gather(*state.tasks, return_exceptions=True), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [30, 59, 60])
async def test_only_latest_synchronously_queued_progress_is_sent(display, minutes):
    drop = reward()
    display.manager.update(drop, 3600)
    drop.real_current_minutes = minutes
    display.manager.update(drop, (60 - minutes) * 60)
    assert await drain(display) == [None, None]
    display.emit.assert_awaited_once_with("drop_progress", display.manager.get_current_drop())
    assert not drop.is_claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("redisplay", [False, True])
async def test_claimed_while_queued_is_not_sent_as_active(display, redisplay):
    drop = reward()
    display.manager.update(drop, 60)
    drop.is_claimed = True
    if redisplay:
        display.manager.update(drop, 0)
    await drain(display)
    assert display.manager.get_current_drop() is None
    if redisplay:
        display.emit.assert_awaited_once_with("drop_progress_stop", {})
    else:
        display.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_stop_is_superseded_by_replacement_before_dispatch(display):
    display.manager.stop_timer()
    drop = reward("replacement")
    display.manager.update(drop, 90)
    await drain(display)
    display.emit.assert_awaited_once_with("drop_progress", display.manager.get_current_drop())


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["progress", "stop"])
@pytest.mark.parametrize("ending", ["finish", "failure", "cancel"])
async def test_inflight_send_cannot_finish_after_replacement(display, first, ending):
    entered, release = asyncio.Event(), asyncio.Event()
    active, maximum = 0, 0
    delivered = []
    error = RuntimeError("test broadcaster failure")

    async def emit(event, data):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            if not entered.is_set():
                entered.set()
                await release.wait()
                if ending == "failure":
                    raise error
            delivered.append((event, data))
        finally:
            active -= 1

    display.emit.side_effect = emit
    if first == "progress":
        display.manager.update(reward("old"), 90)
    else:
        display.manager.stop_timer()
    await asyncio.wait_for(entered.wait(), 1)
    replacement = reward("replacement")
    display.manager.update(replacement, 60)
    await asyncio.sleep(0)
    try:
        assert maximum == 1 and not delivered
        if ending == "cancel":
            display.tasks[0].cancel()
        release.set()
        results = await drain(display)
        if ending == "failure":
            assert results[0] is error
        elif ending == "cancel":
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0] is None
        assert results[1] is None
        assert delivered[-1] == ("drop_progress", display.manager.get_current_drop())
        assert len(delivered) == (2 if ending == "finish" else 1)
        assert maximum == 1 and display.manager._current_drop is replacement
    finally:
        release.set()


@pytest.mark.asyncio
async def test_pending_intermediate_updates_are_skipped_after_blocked_send(display):
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def emit(event, data):
        if not entered.is_set():
            entered.set()
            await release.wait()
        delivered.append((event, data))

    display.emit.side_effect = emit
    display.manager.update(reward("old"), 90)
    await asyncio.wait_for(entered.wait(), 1)
    display.manager.stop_timer()
    display.manager.update(reward("intermediate"), 60)
    display.manager.update(reward("latest"), 30)
    release.set()
    await drain(display)
    assert [data.get("drop_id") for _, data in delivered] == ["old", "latest"]


@pytest.mark.asyncio
async def test_claimed_other_model_does_not_retire_pending_replacement(display):
    replacement, claimed = reward(), reward(claimed=True)
    display.manager.update(replacement, 90)
    display.manager.update(claimed, 0)
    await drain(display)
    display.emit.assert_awaited_once_with("drop_progress", display.manager.get_current_drop())
    assert display.manager._current_drop is replacement
