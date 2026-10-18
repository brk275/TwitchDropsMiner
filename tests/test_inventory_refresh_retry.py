"""Deferred refreshes own one bounded wakeup and obey normal client lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.exceptions import ExitRequest
from src.services.inventory_service import InventoryService
from tests.test_cache_clear import _make_cache_test_client, _make_refresh_test_client
from tests.test_inventory_refresh_races import live as live
from tests.test_inventory_refresh_races import reads, refresh


def timers(service, monkeypatch):
    loop, recorded = asyncio.get_running_loop(), []
    original = loop.call_later

    def later(delay, callback, *args, context=None):
        if callback == service._retry_refresh:
            handle = MagicMock()
            recorded.append((delay, callback, handle))
            return handle
        return original(delay, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_later", later)
    return recorded


@pytest.mark.asyncio
async def test_repeated_deferrals_keep_one_owned_wakeup(live, monkeypatch):
    recorded = timers(live.service, monkeypatch)
    live.service._defer_refresh()
    live.service._defer_refresh()
    assert len(recorded) == 1 and recorded[0][0] == 60
    live.twitch.request_inventory_refresh.assert_not_called()
    recorded[0][1]()
    live.twitch.request_inventory_refresh.assert_called_once_with()
    assert live.service._refresh_retry is None
    live.service._defer_refresh()
    assert len(recorded) == 2
    live.service.cancel_refresh_retry()
    live.service.cancel_refresh_retry()
    recorded[1][2].cancel.assert_called_once_with()
    assert live.service._refresh_retry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_new_fetch_cancels_prior_wakeup_before_scheduling_another(live, monkeypatch, conflict):
    recorded = timers(live.service, monkeypatch)
    live.service._defer_refresh()

    async def update(name):
        if name == "details" and conflict:
            live.drop.update_minutes(10, source="websocket")

    reads(live, during=update)
    await refresh(live)
    recorded[0][2].cancel.assert_called_once_with()
    assert len(recorded) == (2 if conflict else 1)
    assert (live.service._refresh_retry is not None) is conflict
    live.twitch.request_inventory_refresh.assert_not_called()


def owner():
    twitch, _, _, _ = _make_cache_test_client()
    twitch._inventory_service = InventoryService(twitch)
    twitch._state = State.IDLE
    twitch._state_change, twitch._schedule_changed, twitch._resume_mining = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )
    twitch._resume_mining.set()
    twitch._browser = None
    return twitch


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clear", "close"])
async def test_cache_clear_and_close_cancel_the_owned_wakeup(monkeypatch, action):
    twitch = owner()
    service = twitch._inventory_service
    recorded = timers(service, monkeypatch)
    service._defer_refresh()
    if action == "clear":
        service.clear_cached_state()
    else:
        twitch.close()
        assert twitch._state is State.EXIT
        service._defer_refresh()
    assert service._refresh_retry is None
    recorded[0][2].cancel.assert_called_once_with()
    assert twitch._state_change.is_set() is (action == "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ExitRequest, RuntimeError])
async def test_run_finally_cancels_deferred_work_even_on_unexpected_failure(monkeypatch, failure):
    twitch = owner()
    twitch.diagnostics = MagicMock()
    twitch.session_controller = SimpleNamespace(start=MagicMock(), stop=AsyncMock())
    twitch.login_browser = twitch.helper = SimpleNamespace(stop=AsyncMock())
    twitch._channel_service = MagicMock()
    twitch._stop_mining_schedule = AsyncMock()
    twitch._run = AsyncMock(side_effect=failure)
    service = twitch._inventory_service
    recorded = timers(service, monkeypatch)
    service._defer_refresh()
    if failure is ExitRequest:
        await twitch.run()
    else:
        with pytest.raises(RuntimeError):
            await twitch.run()
    assert service._refresh_retry is None and twitch._run_task.done()
    recorded[0][2].cancel.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_cache", [False, True])
async def test_wakeup_preserves_mining_hours_and_explicit_clear_priority(monkeypatch, clear_cache):
    twitch = _make_refresh_test_client()
    twitch._inventory_service = InventoryService(twitch)
    service = twitch._inventory_service
    recorded = timers(service, monkeypatch)
    monkeypatch.setattr(twitch, "is_mining_time", lambda: False)
    service._defer_refresh()
    if clear_cache:
        twitch.request_inventory_refresh(clear_cache=True)
    recorded[0][1]()
    twitch._activate_pending_inventory_refresh()
    assert twitch._state is State.IDLE and twitch._inventory_refresh_pending
    assert twitch._clear_cache_pending is clear_cache
    assert service._refresh_retry is None
    monkeypatch.setattr(twitch, "is_mining_time", lambda: True)
    twitch._activate_pending_inventory_refresh()
    assert twitch._state is State.INVENTORY_FETCH
    assert twitch._clear_cache_pending is clear_cache
