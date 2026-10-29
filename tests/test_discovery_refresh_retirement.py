"""Unsubscribed channels cannot publish late refreshes or retake watching."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from tests.channel_refresh_helpers import live, setup_client
from tests.test_channel_refresh_ownership import drain
from tests.test_priority_fallback import _channel


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("route", ["full-cleanup", "partial-cleanup", "tracking-cap", "retained"])
@pytest.mark.parametrize("cancellation", ["normal", "suppressed", "uncancelled"])
async def test_rediscovery_retires_removed_refreshes_without_changing_retained_ones(
    tmp_path, monkeypatch, manual, route, cancellation,
):
    client, high, _, alternate = setup_client(tmp_path, monkeypatch)
    if not manual:
        client.clear_manual_mode("Automatic fixture")
    pending = alternate if manual else high
    pending.acl_based = route != "partial-cleanup"
    pending._stream = None
    client.channels.clear()
    client.channels[pending.id] = pending
    refreshed = _channel(client, 400, {"id": "2", "name": "Low"})
    refreshed.acl_based = False
    monkeypatch.setattr("src.models.channel.ONLINE_DELAY", timedelta(0))
    if route != "full-cleanup":
        client._state = State.CHANNELS_CLEANUP if route == "partial-cleanup" else State.CHANNELS_FETCH
        client._inventory_loaded = True
        client.request_inventory_refresh = MagicMock()
    if route == "tracking-cap":
        monkeypatch.setattr("src.core.client.MAX_CHANNELS", 1)
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(
        side_effect=lambda channels, **kw: list(channels),
    )
    client.get_live_streams = AsyncMock(
        side_effect=lambda game, **kw: [refreshed] if game == refreshed.game else [],
    )
    before = [(drop.real_current_minutes, drop.is_claimed, drop.claim_id)
              for campaign in client.inventory for drop in campaign.drops]
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    finish_task = None

    async def request(operation):
        payload = live(pending.id)
        payload["data"]["user"]["displayName"] = "Fixture"
        payload["data"]["user"]["broadcastSettings"]["game"] = {
            "id": "2" if manual else "1", "name": "Low" if manual else "High",
        }
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if cancellation == "normal":
                raise
            if cancellation == "uncancelled":
                asyncio.current_task().uncancel()
            await release.wait()
        return payload

    client.gql_request = AsyncMock(side_effect=request)
    pending.check_online()
    worker = pending._stream_refresh_task

    async def finish():
        try:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            observed.append(client.watching_channel.get_with_default(None))
        finally:
            client.close()

    def batch(channels):
        nonlocal finish_task
        finish_task = asyncio.create_task(finish())

    client.gui.channels.batch_update.side_effect = batch
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(client._run(), 3)
        if finish_task is not None:
            await asyncio.wait_for(finish_task, 1)
        retained = route == "retained"
        assert len(observed) == 1 and observed[0] is (pending if retained else refreshed)
        assert (pending.id in client.channels) is retained
        assert pending.online is retained
        assert worker.done()
        client.gql_request.assert_awaited_once()
        client.gui.channels.remove.assert_not_called()
        assert client.gui.channels.batch_update.call_count == 1
        assert client.settings.games_to_watch == ["High", "Low"]
        assert client.is_manual_mode() is manual
        assert client.drop_history.total_count == 0
        assert [(drop.real_current_minutes, drop.is_claimed, drop.claim_id)
                for campaign in client.inventory for drop in campaign.drops] == before
    finally:
        release.set()
        if finish_task is not None:
            await asyncio.gather(finish_task, return_exceptions=True)
        await drain(client, worker)
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["managed", "direct", "bulk"])
@pytest.mark.parametrize("initially_online", [False, True])
@pytest.mark.parametrize("cancellation", ["normal", "suppressed", "uncancelled"])
async def test_topic_removal_retires_only_requested_channel_without_individual_gui_changes(
    tmp_path, monkeypatch, mode, initially_online, cancellation,
):
    client, target, selected, _ = setup_client(tmp_path, monkeypatch)
    client.websocket.remove_topics = MagicMock()
    monkeypatch.setattr("src.models.channel.ONLINE_DELAY", timedelta(0))
    if not initially_online:
        target._stream = None
    confirmed, generation = target._stream, target._refresh_generation
    other_generation = selected._refresh_generation
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(operation):
        payload = live(target.id)
        payload["data"]["user"]["displayName"] = "Fixture"
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if cancellation == "normal":
                raise
            if cancellation == "uncancelled":
                asyncio.current_task().uncancel()
            await release.wait()
        return [payload] if mode == "bulk" else payload

    client.gql_request = AsyncMock(side_effect=request)
    if mode == "managed":
        target.check_online()
        worker = target._stream_refresh_task
    else:
        worker = asyncio.create_task(
            target.update_stream() if mode == "direct" else client.bulk_check_online([target]),
        )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        client.gui.channels.reset_mock()
        client._remove_channel_topics(iter([target]))
        assert target._stream is confirmed
        assert target._refresh_generation != generation
        assert selected._refresh_generation == other_generation
        if mode == "managed":
            assert worker in client._channel_tasks
            assert target._stream_refresh_task is None
        client.websocket.remove_topics.assert_called_once_with([
            f"video-playback-by-id.{target.id}", f"broadcast-settings-update.{target.id}",
        ])
        client.gui.channels.remove.assert_not_called()
        client.gui.channels.display.assert_not_called()
        release.set()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
        assert target._stream is confirmed
        assert client.watching_channel.get_with_default(None) is selected
        client.gql_request.assert_awaited_once()
    finally:
        release.set()
        await drain(client, worker)


def test_empty_topic_removal_does_not_touch_unrelated_channels(tmp_path, monkeypatch):
    client, target, selected, alternate = setup_client(tmp_path, monkeypatch)
    client.websocket.remove_topics = MagicMock()
    generations = [channel._refresh_generation for channel in (target, selected, alternate)]
    client.gui.channels.reset_mock()

    client._remove_channel_topics(iter([]))

    assert [channel._refresh_generation for channel in (target, selected, alternate)] == generations
    client.websocket.remove_topics.assert_not_called()


@pytest.mark.asyncio
async def test_manual_target_clear_during_discovery_does_not_replace_reentrant_exit(tmp_path, monkeypatch):
    client, *_ = setup_client(tmp_path, monkeypatch)
    client.channels.clear()
    client._state = State.CHANNELS_FETCH
    client._inventory_loaded = True
    client.request_inventory_refresh = MagicMock()
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.bulk_check_online = AsyncMock()
    client.get_live_streams = AsyncMock(return_value=[])
    client._channel_service.filter_banned_channels = AsyncMock(side_effect=lambda channels, **kw: list(channels))
    client.watch = MagicMock(wraps=client.watch)
    published = []

    def publish(info):
        published.append(info)
        client.close()

    client.gui.broadcast_manual_mode_change.side_effect = publish
    try:
        await asyncio.wait_for(client._run(), 1)
        assert client._state is State.EXIT and client._state_change.is_set()
        assert published == [{"active": False}]
        client.watch.assert_not_called()
        client.gql_request.assert_not_awaited()
    finally:
        client.close()
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)
        client._channel_service.clear_bans()
    client.gui.channels.remove.assert_not_called()
    client.gui.channels.display.assert_not_called()
