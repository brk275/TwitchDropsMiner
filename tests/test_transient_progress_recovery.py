"""Transient progress failures must not stop watching or create estimated progress."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.auth.session_bundle import SessionError
from src.config import State
from tests.test_imported_session_retry import immediate_delays, running_miner
from tests.test_stale_progress import make_campaign


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http_5xx", "connection", "timeout"])
async def test_transient_exhaustion_keeps_watching_until_next_real_reading(
    tmp_path, monkeypatch, kind,
):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        channel, campaign, clock, sleep, snapshots = prepare_watch(client)
        success = {"data": {"currentUser": {"dropCurrentSession": {
            "dropID": "drop", "currentMinutesWatched": 7,
        }}}}
        if kind == "http_5xx":
            peer.responses.extend([503, 502, 504, success])
        else:
            peer.responses.append(success)
            http = client._browser._transport._http
            original_request = http.request
            attempts = []

            def flaky_request(*args, **kwargs):
                attempts.append(True)
                if len(attempts) <= 3:
                    error = TimeoutError if kind == "timeout" else aiohttp.ClientConnectionError
                    raise error("Synthetic transport failure")
                return original_request(*args, **kwargs)

            monkeypatch.setattr(http, "request", flaky_request)
        delays = immediate_delays(client._browser, monkeypatch)
        query = client.gql_request
        polls = []

        async def observe_query(operation):
            polls.append(clock[0])
            return await query(operation)

        monkeypatch.setattr(client, "gql_request", observe_query)
        target = campaign.timed_drops["drop"]
        with (
            patch.object(client, "close", wraps=client.close) as close,
            patch.object(campaign, "bump_minutes", wraps=campaign.bump_minutes) as bumps,
            patch.object(target, "update_minutes", wraps=target.update_minutes) as updates,
            patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
            patch.object(client._watch_service, "watch_sleep", side_effect=sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(client._watch_service.watch_loop(), 2)
        close.assert_not_called()
        bumps.assert_not_called()
        assert [call.args[0] for call in updates.call_args_list] == [7]
        assert polls == [20, 80]
        assert delays == [1, 2]
        assert all(real == 10 for at, real, _extra in snapshots if at < 80)
        assert all(extra == 0 for _at, _real, extra in snapshots)
        assert all(drop.real_current_minutes == 7 for drop in campaign.drops)
        assert all(drop.extra_current_minutes == 0 for drop in campaign.drops)
        assert channel.send_watch.await_count == 10
        assert client._browser.status()["state"] == "ready"
        assert client._state is not State.EXIT
        assert not peer.responses
        assert len(peer.calls) == (4 if kind == "http_5xx" else 1)
        if kind != "http_5xx":
            assert len(attempts) == 4


@pytest.mark.asyncio
async def test_repeated_outage_preserves_cadence_without_estimated_progress(tmp_path, monkeypatch):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        channel, campaign, clock, sleep, snapshots = prepare_watch(client, stop_at=160)
        peer.responses.extend([503] * 9)
        delays = immediate_delays(client._browser, monkeypatch)
        query = client.gql_request
        polls = []

        async def observe_query(operation):
            polls.append(clock[0])
            return await query(operation)

        monkeypatch.setattr(client, "gql_request", observe_query)
        with (
            patch.object(client, "close", wraps=client.close) as close,
            patch.object(campaign, "bump_minutes", wraps=campaign.bump_minutes) as bumps,
            patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
            patch.object(client._watch_service, "watch_sleep", side_effect=sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(client._watch_service.watch_loop(), 2)
        close.assert_not_called()
        bumps.assert_not_called()
        assert polls == [20, 80, 140]
        assert delays == [1, 2] * 3
        assert len(peer.calls) == 9 and not peer.responses
        assert channel.send_watch.await_count == 16
        assert all(real == 10 and extra == 0 for _at, real, extra in snapshots)
        assert all(drop.real_current_minutes == 10 and drop.extra_current_minutes == 0
                   for drop in campaign.drops)
        assert client._browser.status()["state"] == "ready"
        assert client._state is not State.EXIT


@pytest.mark.asyncio
@pytest.mark.parametrize("code, reason, status", [
    ("AUTH", None, None), ("STALE", None, None), ("FORMAT", None, None),
    ("REQUEST", "tls", None), ("REQUEST", "invalid_json", None),
    ("REQUEST", "http_error", 400), ("REQUEST", "http_error", 429),
])
async def test_nontransient_errors_keep_existing_failure_boundary(
    tmp_path, monkeypatch, code, reason, status,
):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        _channel, campaign, clock, sleep, _snapshots = prepare_watch(client)
        error = SessionError(code, reason=reason, upstream_status=status)
        client.gql_request = AsyncMock(side_effect=error)
        with (
            patch.object(client, "close", wraps=client.close) as close,
            patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
            patch.object(client._watch_service, "watch_sleep", side_effect=sleep),
            pytest.raises(SessionError) as caught,
        ):
            await asyncio.wait_for(client._watch_service.watch_loop(), 2)
        assert caught.value is error
        close.assert_called_once_with()
        client.gql_request.assert_awaited_once()
        assert client._state is State.EXIT
        assert not peer.calls
        assert all(drop.real_current_minutes == 10 and drop.extra_current_minutes == 0
                   for drop in campaign.drops)


@pytest.mark.asyncio
async def test_cancellation_drains_inflight_read_without_closing(tmp_path, monkeypatch):
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _clock):
        _channel, campaign, clock, sleep, _snapshots = prepare_watch(client)
        entered, drained = asyncio.Event(), asyncio.Event()

        async def blocked_query(operation):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        client.gql_request = blocked_query
        with (
            patch.object(client, "close", wraps=client.close) as close,
            patch("src.services.watch_service.monotonic", side_effect=lambda: clock[0]),
            patch.object(client._watch_service, "watch_sleep", side_effect=sleep),
        ):
            task = asyncio.create_task(client._watch_service.watch_loop())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert drained.is_set()
        close.assert_not_called()
        assert client._state is not State.EXIT
        assert not peer.calls
        assert all(drop.real_current_minutes == 10 and drop.extra_current_minutes == 0
                   for drop in campaign.drops)


def prepare_watch(client, *, stop_at=100):
    channel = MagicMock()
    channel.id = 123
    channel.online = True
    channel.banned = False
    channel.drops_enabled = True
    channel._watch_current.return_value = True
    watches = [0]

    async def send_watch():
        watches[0] += 1
        assert watches[0] <= 20, "Bound the synthetic watch harness"
        return True

    channel.send_watch = AsyncMock(side_effect=send_watch)
    campaign = make_campaign(client)
    channel.game = campaign.game
    client.inventory = [campaign]
    client._drops = dict(campaign.timed_drops)
    client.wanted_games = [campaign.game]
    client.is_mining_time = MagicMock(return_value=True)
    client.watching_channel.set(channel)
    client._state = State.CHANNEL_SWITCH
    assert client._watch_service.can_watch(channel)
    assert client._inventory_service.get_active_campaign(channel) is campaign
    clock, snapshots = [0.0], []

    async def sleep(delay):
        assert delay >= 0
        target = campaign.timed_drops["drop"]
        snapshots.append((clock[0], target.real_current_minutes, target.extra_current_minutes))
        clock[0] += delay
        if clock[0] >= stop_at:
            raise asyncio.CancelledError

    return channel, campaign, clock, sleep, snapshots
