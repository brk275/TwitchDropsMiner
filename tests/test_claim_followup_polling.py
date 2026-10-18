"""Unusable post-claim reads must not prevent the normal continuation decision."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from src.auth.session_bundle import SessionError
from src.config import GQL_OPERATIONS
from src.drop_history import DropHistory
from src.exceptions import GQLException, GQLInvalidResponse, LoginException
from src.services import message_handlers
from src.services.message_handlers import MessageHandlerService
from tests.test_current_drop_responses import response
from tests.test_imported_session_retry import TwitchPeer, running_miner
from tests.test_inventory_refresh_races import installed
from tests.test_stale_progress import mining_state as mining_state


CONFIRMED = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
SAME = response({"dropID": "drop", "currentMinutesWatched": 10})
BAD = [None, False, 7, "private-followup-canary", [], {}, {"data": None}, {"data": []},
       {"data": {}}, {"data": {"currentUser": None}}, {"data": {"currentUser": []}},
       {"data": {"currentUser": {}}}, response(False), response([]), response("private-followup-canary"),
       response({}), response({"currentMinutesWatched": 10}),
       response({"dropID": "", "currentMinutesWatched": 10}),
       response({"dropID": False, "currentMinutesWatched": 10}),
       response({"dropID": [], "currentMinutesWatched": 10}),
       response({"dropID": "drop"}), response({"dropID": "drop", "currentMinutesWatched": None}),
       response({"dropID": "drop", "currentMinutesWatched": "private-followup-canary"}),
       *({**deepcopy(SAME), "errors": value} for value in (False, {}, "private-followup-canary", [{"message": "private-followup-canary"}])),
       {**deepcopy(SAME), "error": "private-followup-canary"}]


def event(drop):
    return {"type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": "new-opaque-claim"}}


@pytest.fixture
def claiming(mining_state, tmp_path, monkeypatch):
    twitch, channel, campaign = mining_state
    drop = campaign.timed_drops["drop"]
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    twitch.restart_watching = MagicMock()
    twitch.request_inventory_refresh = MagicMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    for reward in campaign.drops:
        reward.extra_current_minutes = 2
    original = tuple((reward.progress_observation, reward.current_minutes) for reward in campaign.drops)
    timing = SimpleNamespace(sleep=AsyncMock(), wait_for=asyncio.wait_for)
    # Replace this module's reference, not asyncio.sleep globally or limiter timers.
    monkeypatch.setattr(message_handlers, "asyncio", timing)
    return SimpleNamespace(twitch=twitch, channel=channel, campaign=campaign, drop=drop,
                           timing=timing, original=original, history_dir=tmp_path)


def queue(state, followups, *, confirmed=CONFIRMED):
    state.twitch.gql_request = AsyncMock(side_effect=[deepcopy(confirmed), *followups])


def assert_settled(state):
    twitch, campaign, drop = state.twitch, state.campaign, state.drop
    assert drop.is_claimed is True and drop.claim_id == "new-opaque-claim"
    assert drop.current_minutes == 60 and drop.extra_current_minutes == 0
    assert campaign.timed_drops["sibling"].current_minutes == state.original[1][1]
    assert tuple(reward.progress_observation for reward in campaign.drops) == tuple(item[0] for item in state.original)
    assert campaign.progress_revision == 1 and twitch._claim_requests == 0 and not campaign._pending_claims
    assert [item["id"] for item in DropHistory(state.history_dir).get_entries()] == [drop.id]
    twitch.drop_history.record.assert_called_once_with(drop, campaign)
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()


def assert_continuation(state, restart):
    if restart:
        state.twitch.restart_watching.assert_called_once_with()
        state.twitch.request_inventory_refresh.assert_not_called()
    else:
        state.twitch.restart_watching.assert_not_called()
        state.twitch.request_inventory_refresh.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("bad", [*BAD, GQLException("private-followup-canary"), GQLInvalidResponse(), TimeoutError("private-followup-canary")])
async def test_unusable_followup_keeps_settled_claim_and_makes_normal_decision(claiming, bad, restart, caplog):
    state = claiming
    if not restart:
        state.channel.game = None
    original = repr(bad)
    queue(state, [bad, response(None)])
    await MessageHandlerService(state.twitch).process_drops(42, event(state.drop))
    assert_settled(state)
    assert_continuation(state, restart)
    assert [call.args[0]["operationName"] for call in state.twitch.gql_request.await_args_list] == [
        GQL_OPERATIONS["ClaimDrop"]["operationName"], GQL_OPERATIONS["CurrentDrop"]["operationName"],
    ]
    state.timing.sleep.assert_awaited_once_with(4)
    assert repr(bad) == original and "private-followup-canary" not in caplog.text
    assert "Exception in" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("followups, delays", [
    ([response(None)], [4]),
    ([response({"dropID": "sibling", "currentMinutesWatched": 10})], [4]),
    ([deepcopy(SAME), response(None)], [4, 2]),
    ([deepcopy(SAME)] * 8, [4] + [2] * 8),
])
async def test_valid_followup_preserves_existing_bounded_waits_and_no_progress_credit(claiming, followups, delays):
    state = claiming
    queue(state, followups)
    await MessageHandlerService(state.twitch).process_drops(42, event(state.drop))
    assert_settled(state)
    assert_continuation(state, True)
    assert state.twitch.gql_request.await_count == 1 + len(followups)
    assert [call.args[0] for call in state.timing.sleep.await_args_list] == delays
    for call in state.twitch.gql_request.await_args_list[1:]:
        assert call.args == (GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": "123"}),)


@pytest.mark.asyncio
async def test_repeated_claim_events_do_not_replay_claim_after_bad_followup(claiming):
    state = claiming
    queue(state, [BAD[0], BAD[9]])
    service = MessageHandlerService(state.twitch)
    await service.process_drops(42, event(state.drop))
    await service.process_drops(42, event(state.drop))
    assert_settled(state)
    assert state.twitch.restart_watching.call_count == 2
    assert [call.args[0]["operationName"] for call in state.twitch.gql_request.await_args_list] == [
        GQL_OPERATIONS["ClaimDrop"]["operationName"], *[GQL_OPERATIONS["CurrentDrop"]["operationName"]] * 2,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LoginException("auth"), SessionError("STALE"), RuntimeError("unexpected"), ValueError("not a parser error"), asyncio.CancelledError()])
async def test_non_query_failures_and_cancellation_are_not_swallowed(claiming, error):
    state = claiming
    queue(state, [error])
    with pytest.raises(type(error)) as caught:
        await MessageHandlerService(state.twitch).process_drops(42, event(state.drop))
    assert caught.value is error
    assert_settled(state)
    state.twitch.restart_watching.assert_not_called()
    state.twitch.request_inventory_refresh.assert_not_called()
    assert state.twitch.gql_request.await_count == 2


@pytest.mark.asyncio
async def test_pending_followup_is_time_bounded_and_cancelled_before_continuation(claiming):
    state, entered, cancelled = claiming, asyncio.Event(), asyncio.Event()
    actual_wait_for = asyncio.wait_for
    timeouts = []

    async def bounded(awaitable, *, timeout):
        timeouts.append(timeout)
        return await actual_wait_for(awaitable, timeout=0.01)

    async def query(operation):
        if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return deepcopy(CONFIRMED)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.timing.wait_for = bounded
    state.twitch.gql_request = AsyncMock(side_effect=query)
    task = asyncio.create_task(MessageHandlerService(state.twitch).process_drops(42, event(state.drop)))
    try:
        await actual_wait_for(entered.wait(), timeout=1)
        await actual_wait_for(task, timeout=0.2)
        assert timeouts == [5] and cancelled.is_set()
        assert_settled(state)
        assert_continuation(state, True)
        assert state.twitch.gql_request.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_handler_cancels_pending_followup_without_selecting(claiming):
    state, entered, cancelled = claiming, asyncio.Event(), asyncio.Event()

    async def query(operation):
        if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return deepcopy(CONFIRMED)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.twitch.gql_request = AsyncMock(side_effect=query)
    task = asyncio.create_task(MessageHandlerService(state.twitch).process_drops(42, event(state.drop)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert_settled(state)
        state.twitch.restart_watching.assert_not_called()
        state.twitch.request_inventory_refresh.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [None, GQLException("private-followup-canary")])
async def test_failed_claim_is_not_promoted_to_success_by_followup_recovery(claiming, followup):
    state = claiming
    queue(state, [followup], confirmed={"data": {"claimDropRewards": {"status": "NOT_ELIGIBLE"}}})
    await MessageHandlerService(state.twitch).process_drops(42, event(state.drop))
    assert state.drop.is_claimed is False and state.campaign.progress_revision == 0
    assert tuple((drop.progress_observation, drop.current_minutes) for drop in state.campaign.drops) == state.original
    assert state.twitch._claim_requests == 0 and not state.campaign._pending_claims
    assert state.twitch.drop_history.total_count == 0
    state.twitch.drop_history.record.assert_not_called()
    state.twitch.request_games_update.assert_not_called()
    state.twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    state.drop._send_telegram_notification.assert_not_awaited()
    state.drop._send_discord_notification.assert_not_awaited()
    assert state.twitch.gql_request.await_count == 2
    assert_continuation(state, True)


@pytest.mark.asyncio
async def test_no_selected_channel_keeps_existing_no_followup_path(claiming):
    state = claiming
    state.twitch.watching_channel.clear()
    queue(state, [])
    await MessageHandlerService(state.twitch).process_drops(42, event(state.drop))
    assert_settled(state)
    assert_continuation(state, True)
    state.twitch.gql_request.assert_awaited_once_with(
        GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "new-opaque-claim"}}),
    )
    state.timing.sleep.assert_awaited_once_with(4)


def imported_target(twitch, tmp_path, monkeypatch):
    owner = installed(twitch, tmp_path)
    channel = MagicMock()
    channel.id, channel.game = 123, owner.previous.game
    channel.online, channel.banned = True, False
    twitch.watching_channel.set(channel)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    timing = SimpleNamespace(sleep=AsyncMock(), wait_for=asyncio.wait_for)
    monkeypatch.setattr(message_handlers, "asyncio", timing)
    return owner, timing


def assert_imported_claim_finished(twitch, owner, observed):
    drop = owner.drop
    assert drop.is_claimed is True and drop.current_minutes == 60 and drop.progress_observation == observed
    assert drop.campaign.progress_revision == 1 and twitch.drop_history.total_count == 1
    twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    assert twitch._claim_requests == 0 and not drop.campaign._pending_claims
    assert twitch._inventory_refresh_pending and twitch._games_update_pending
    assert twitch._browser.status()["state"] == "ready" and twitch._browser.status()["generation"] == 1
    assert not twitch._browser._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {"data": {"currentUser": None}},
                               {**deepcopy(SAME), "errors": False},
                               response({"dropID": "known-drop", "currentMinutesWatched": "private-followup-canary"})])
async def test_actual_imported_http_keeps_claim_and_queues_refresh_after_bad_followup(tmp_path, monkeypatch, bad, caplog):
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        owner, timing = imported_target(twitch, tmp_path, monkeypatch)
        observed = owner.drop.progress_observation
        peer.responses.extend([deepcopy(CONFIRMED), deepcopy(bad)])
        await MessageHandlerService(twitch).process_drops(42, event(owner.drop))
        assert_imported_claim_finished(twitch, owner, observed)
        assert [operation["operationName"] for operation in peer.calls] == [
            GQL_OPERATIONS["ClaimDrop"]["operationName"], GQL_OPERATIONS["CurrentDrop"]["operationName"],
        ]
        assert not peer.responses and not owner.service.refresh_retry_pending
        timing.sleep.assert_awaited_once_with(4)
        assert "private-followup-canary" not in caplog.text and "Exception in" not in caplog.text


@pytest.mark.asyncio
async def test_actual_imported_http_timeout_releases_transport_for_next_explicit_read(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original_query, actual_wait_for = TwitchPeer.gql, asyncio.wait_for

    async def delayed(peer, request):
        body = await request.json()
        if peer.armed and body.get("operationName") == GQL_OPERATIONS["CurrentDrop"]["operationName"] and not entered.is_set():
            peer.calls.append(body)
            entered.set()
            await release.wait()
            return web.json_response(response(None))
        return await original_query(peer, request)

    monkeypatch.setattr(TwitchPeer, "gql", delayed)
    async with running_miner(tmp_path, monkeypatch) as (twitch, peer, _):
        owner, timing = imported_target(twitch, tmp_path, monkeypatch)
        observed, timeouts = owner.drop.progress_observation, []

        async def bounded(awaitable, *, timeout):
            timeouts.append(timeout)
            return await actual_wait_for(awaitable, timeout=0.1)

        timing.wait_for = bounded
        peer.responses.extend([deepcopy(CONFIRMED), response(None)])
        try:
            await actual_wait_for(MessageHandlerService(twitch).process_drops(42, event(owner.drop)), timeout=1)
            assert entered.is_set() and timeouts == [5]
            assert_imported_claim_finished(twitch, owner, observed)
            assert len(peer.calls) == 2 and len(peer.responses) == 1
            release.set()
            assert await twitch.gql_request(GQL_OPERATIONS["CurrentDrop"].with_variables({"channelID": "123"})) == response(None)
            assert len(peer.calls) == 3 and not peer.responses
            assert_imported_claim_finished(twitch, owner, observed)
        finally:
            release.set()
