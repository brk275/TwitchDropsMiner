"""Advisory post-claim reads cannot prevent normal mining continuation."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.session_bundle import SessionError
from src.config import GQL_OPERATIONS
from src.drop_history import DropHistory
from src.exceptions import GQLException, GQLInvalidResponse
from src.services import message_handlers
from src.services.message_handlers import MessageHandlerService
from tests.test_current_drop_responses import response
from tests.test_imported_session_retry import immediate_delays, running_miner
from tests.test_stale_progress import mining_state as mining_state


CONFIRMED = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
SAME = response({"dropID": "drop", "currentMinutesWatched": 10})
BAD = [None, False, 7, "private-followup", [], {}, {"data": None},
       {"data": {"currentUser": None}}, response(False), response([]), response({}),
       response({"dropID": "drop"}), response({"dropID": [], "currentMinutesWatched": 10}),
       response({"dropID": "drop", "currentMinutesWatched": True}),
       {**SAME, "errors": [{"message": "private-followup"}]},
       GQLException("private-followup"), GQLInvalidResponse(), TimeoutError("private-followup")]


@pytest.fixture
def claiming(mining_state, tmp_path, monkeypatch):
    twitch, channel, campaign = mining_state
    twitch.drop_history = DropHistory(tmp_path)
    twitch.drop_history.record = MagicMock(wraps=twitch.drop_history.record)
    twitch.restart_watching = MagicMock()
    twitch.request_inventory_refresh = MagicMock()
    drop = campaign.timed_drops["drop"]
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    timing = SimpleNamespace(sleep=AsyncMock(), wait_for=asyncio.wait_for)
    monkeypatch.setattr(message_handlers, "asyncio", timing)
    event = {"type": "drop-claim", "data": {"drop_id": drop.id, "drop_instance_id": "new-claim"}}
    return SimpleNamespace(twitch=twitch, channel=channel, campaign=campaign,
                           drop=drop, timing=timing, event=event, directory=tmp_path)


def settled(state):
    assert state.drop.is_claimed
    assert (state.drop.real_current_minutes, state.drop.extra_current_minutes) == (60, 0)
    sibling = state.campaign.timed_drops["sibling"]
    assert (sibling.real_current_minutes, sibling.extra_current_minutes) == (10, 0)
    assert state.campaign.progress_revision == 1
    assert [row["id"] for row in DropHistory(state.directory).get_entries()] == [state.drop.id]
    state.twitch.drop_history.record.assert_called_once_with(state.drop, state.campaign)
    state.twitch.request_games_update.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("bad", BAD)
async def test_unusable_followup_keeps_claim_and_continues(claiming, restart, bad, caplog):
    state = claiming
    if not restart:
        state.channel.game = None
    state.twitch.gql_request = AsyncMock(side_effect=[deepcopy(CONFIRMED), bad])
    await MessageHandlerService(state.twitch).process_drops(42, state.event)
    settled(state)
    assert state.twitch.restart_watching.call_count == int(restart)
    assert state.twitch.request_inventory_refresh.call_count == int(not restart)
    assert [call.args[0]["operationName"] for call in state.twitch.gql_request.await_args_list] == [
        GQL_OPERATIONS["ClaimDrop"]["operationName"], GQL_OPERATIONS["CurrentDrop"]["operationName"],
    ]
    state.timing.sleep.assert_awaited_once_with(4)
    assert "private-followup" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("replies,delays", [
    ([response(None)], [4]),
    ([response({"dropID": "sibling", "currentMinutesWatched": 50})], [4]),
    ([SAME, response(None)], [4, 2]),
    ([SAME] * 8, [4] + [2] * 8),
])
async def test_valid_followup_preserves_waits_without_progress_credit(claiming, replies, delays):
    state = claiming
    state.twitch.gql_request = AsyncMock(side_effect=[deepcopy(CONFIRMED), *deepcopy(replies)])
    await MessageHandlerService(state.twitch).process_drops(42, state.event)
    settled(state)
    state.twitch.restart_watching.assert_called_once_with()
    assert [call.args[0] for call in state.timing.sleep.await_args_list] == delays
    assert state.twitch.gql_request.await_count == 1 + len(replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [SessionError("AUTH"), SessionError("STALE"),
                                  RuntimeError("unexpected"), ValueError("query error"), asyncio.CancelledError()])
async def test_auth_unexpected_errors_and_cancellation_propagate(claiming, error):
    state = claiming
    state.twitch.gql_request = AsyncMock(side_effect=[deepcopy(CONFIRMED), error])
    with pytest.raises(type(error)) as caught:
        await MessageHandlerService(state.twitch).process_drops(42, state.event)
    assert caught.value is error
    settled(state)
    state.twitch.restart_watching.assert_not_called()
    state.twitch.request_inventory_refresh.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_read_is_drained_before_timeout_continuation_or_cancel(claiming, cancel):
    state, entered, drained = claiming, asyncio.Event(), asyncio.Event()
    actual_wait_for = asyncio.wait_for
    timeouts = []

    async def query(operation):
        if operation["operationName"] == GQL_OPERATIONS["ClaimDrop"]["operationName"]:
            return deepcopy(CONFIRMED)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def bounded(awaitable, *, timeout):
        timeouts.append(timeout)
        return await actual_wait_for(awaitable, timeout=0.01 if not cancel else 10)

    state.twitch.gql_request = AsyncMock(side_effect=query)
    state.timing.wait_for = bounded
    task = asyncio.create_task(MessageHandlerService(state.twitch).process_drops(42, state.event))
    try:
        await actual_wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await actual_wait_for(task, 1)
        assert drained.is_set() and timeouts == [5]
        settled(state)
        assert state.twitch.restart_watching.call_count == int(not cancel)
        state.twitch.request_inventory_refresh.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_imported_request_exhaustion_does_not_lose_post_claim_restart(claiming, tmp_path, monkeypatch):
    state = claiming
    async with running_miner(tmp_path / "session", monkeypatch) as (client, peer, _):
        peer.responses.extend([deepcopy(CONFIRMED), 503, 502, 504])
        waited = immediate_delays(client._browser, monkeypatch)
        state.twitch.gql_request = client.gql_request
        await MessageHandlerService(state.twitch).process_drops(42, state.event)
        settled(state)
        state.twitch.restart_watching.assert_called_once_with()
        assert len(peer.calls) == 4 and not peer.responses
        assert waited == [1, 2]
        assert client._browser.status()["state"] == "ready"
        assert not client._browser._lock.locked()


@pytest.mark.asyncio
async def test_failed_claim_is_not_promoted_by_advisory_read_recovery(claiming):
    state = claiming
    state.twitch.gql_request = AsyncMock(side_effect=[
        {"data": {"claimDropRewards": {"status": "NOT_ELIGIBLE"}}}, GQLInvalidResponse(),
    ])
    await MessageHandlerService(state.twitch).process_drops(42, state.event)
    assert not state.drop.is_claimed
    assert state.drop.real_current_minutes == 10
    assert state.campaign.progress_revision == 0
    assert DropHistory(state.directory).total_count == 0
    state.twitch.drop_history.record.assert_not_called()
    state.twitch.request_games_update.assert_not_called()
    state.twitch.restart_watching.assert_called_once_with()
    assert state.twitch.gql_request.await_count == 2


@pytest.mark.asyncio
async def test_duplicate_event_does_not_resend_claim_or_duplicate_history(claiming):
    state = claiming
    state.twitch.gql_request = AsyncMock(side_effect=[deepcopy(CONFIRMED), None, None])
    service = MessageHandlerService(state.twitch)
    await service.process_drops(42, state.event)
    await service.process_drops(42, state.event)
    settled(state)
    assert state.twitch.restart_watching.call_count == 2
    assert [call.args[0]["operationName"] for call in state.twitch.gql_request.await_args_list] == [
        GQL_OPERATIONS["ClaimDrop"]["operationName"], *[GQL_OPERATIONS["CurrentDrop"]["operationName"]] * 2,
    ]
    state.drop._send_telegram_notification.assert_awaited_once_with()
    state.drop._send_discord_notification.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_no_channel_does_not_send_advisory_reads(claiming):
    state = claiming
    state.twitch.watching_channel.clear()
    state.twitch.gql_request = AsyncMock(return_value=deepcopy(CONFIRMED))
    await MessageHandlerService(state.twitch).process_drops(42, state.event)
    settled(state)
    state.twitch.gql_request.assert_awaited_once()
    state.timing.sleep.assert_awaited_once_with(4)
    state.twitch.restart_watching.assert_called_once_with()
