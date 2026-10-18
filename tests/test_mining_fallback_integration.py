"""Selected campaign guesses and complete estimate batches work together."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_EXTRA_MINUTES, State
from src.core.client import Twitch
from src.exceptions import GQLException
from src.models.channel import Channel
from src.services.inventory_service import InventoryService
from src.services.message_handlers import MessageHandlerService
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_drop_ignore_policy import _drop
from tests.test_priority_fallback import _campaign, _channel, _settings


@pytest.fixture
def mining_pair():
    client = MagicMock()
    client.settings = _settings("Wanted", priority_list_only=True)
    client.is_mining_time.return_value = True
    wanted = _campaign(
        "wanted", "Wanted", settings=client.settings, drops=[
            _drop("base", "Wanted foundation", has_benefit=False, required_minutes=60),
            _drop("left", "Wanted left", preconditions=("base",), required_minutes=100),
            _drop("right", "Wanted right", preconditions=("base",), required_minutes=100),
            _drop("parallel", "Wanted parallel", required_minutes=200),
        ],
    )
    unwanted = _campaign(
        "unwanted", "Special", game_id=509663, settings=client.settings,
        drops=[_drop("other", "Other reward", required_minutes=1)], acl=(100,),
    )
    client.inventory = [unwanted, wanted]
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client.wanted_games = [wanted.game]
    current = _channel(client, 100, {"id": "1", "name": "Wanted"})
    client.watching_channel = AwaitableValue()
    client.watching_channel.set(current)
    client._inventory_service = InventoryService(client)
    client._drops = {drop.id: drop for campaign in client.inventory for drop in campaign.drops}
    return client, wanted, unwanted, current


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_claimed", [False, True])
@pytest.mark.parametrize("reported", [False, True])
async def test_watch_loop_combines_selected_guesses_complete_batches_and_prerequisites(
    mining_pair, monkeypatch, parent_claimed, reported,
):
    client, wanted, unwanted, current = mining_pair
    base = wanted.timed_drops["base"]
    if parent_claimed:
        base.is_claimed = True
        base.real_current_minutes = base.required_minutes
    limited = wanted.timed_drops["left"] if parent_claimed else base
    limited.extra_current_minutes = MAX_EXTRA_MINUTES - 1
    eligible = {drop.id for drop in wanted.drops if drop.can_earn(current)}
    before_real = {drop.id: drop.real_current_minutes for drop in wanted.drops}
    assert wanted.can_earn(current) and unwanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is wanted

    progress = {"dropID": "parallel", "currentMinutesWatched": 5} if reported else None
    client.gql_request = AsyncMock(return_value={"data": {"currentUser": {"dropCurrentSession": progress}}})
    client.gui.progress.minute_almost_done.return_value = True
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(side_effect=[True, asyncio.CancelledError()]))
    clock = [0]
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    watcher = WatchService(client)

    async def sleep_once(delay):
        clock[0] = 20

    watcher.watch_sleep = sleep_once
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch_loop(), 1)

    client.gql_request.assert_awaited_once()
    for drop in wanted.drops:
        expected_extra = 0
        if not reported and drop.id in eligible:
            expected_extra = MAX_EXTRA_MINUTES if drop is limited else 1
        assert drop.extra_current_minutes == expected_extra
        assert drop.real_current_minutes == before_real[drop.id] + (5 if reported and drop.id in eligible else 0)
    assert unwanted.timed_drops["other"].real_current_minutes == 0
    assert unwanted.timed_drops["other"].extra_current_minutes == 0
    if reported:
        client.change_state.assert_not_called()
    else:
        client.change_state.assert_called_once_with(State.CHANNEL_SWITCH)
    assert wanted.claimed_drops == int(parent_claimed)


@pytest.mark.parametrize("blocker", ["ignored", "expired", "claimed"])
def test_policy_and_expiry_changes_do_not_fall_back_to_an_unwanted_campaign(mining_pair, blocker):
    client, wanted, unwanted, current = mining_pair
    assert client._inventory_service.get_active_campaign(current) is wanted
    if blocker == "ignored":
        client.settings.drop_name_blacklist = ["Wanted"]
    elif blocker == "expired":
        wanted.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    else:
        for drop in wanted.drops:
            drop.is_claimed = True
    assert not wanted.can_earn(current)
    assert unwanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is None
    assert client.wanted_games == [wanted.game]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["fresh", "stale", "null", "error", "timeout"])
@pytest.mark.parametrize("observed", [0, 10, 11])
async def test_entire_estimate_batch_recovers_without_accepting_an_old_poll(
    mining_pair, monkeypatch, response, observed,
):
    client, wanted, unwanted, current = mining_pair
    capped = [wanted.timed_drops[key] for key in ("base", "parallel")]
    for drop in capped:
        drop.real_current_minutes = 10
        drop.extra_current_minutes = MAX_EXTRA_MINUTES - 1
    wanted.bump_minutes(current)
    assert all(drop.extra_current_minutes == MAX_EXTRA_MINUTES for drop in capped)
    assert not wanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is None
    assert unwanted.can_earn(current)
    client.change_state.reset_mock()
    revision = wanted.progress_revision

    async def progress_query(operation):
        if response != "fresh":
            await MessageHandlerService(client).process_drops(1, {
                "type": "drop-progress", "data": {
                    "drop_id": "parallel", "current_progress_min": observed,
                    "required_progress_min": 200,
                },
            })
        if response == "error":
            raise GQLException("controlled query failure")
        if response == "timeout":
            raise TimeoutError
        session = None if response == "null" else {
            "dropID": "parallel", "currentMinutesWatched": observed if response == "fresh" else 9,
        }
        return {"data": {"currentUser": {"dropCurrentSession": session}}}

    client.gql_request = AsyncMock(side_effect=progress_query)
    client.gui.progress.minute_almost_done.return_value = False
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(side_effect=[True, asyncio.CancelledError()]))
    clock = [0]
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    watcher = WatchService(client)

    async def sleep_once(delay):
        clock[0] = 20

    watcher.watch_sleep = sleep_once
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch_loop(), 1)

    client.gql_request.assert_awaited_once()
    assert [(drop.real_current_minutes, drop.extra_current_minutes) for drop in capped] == [
        (observed, 0), (observed, 0),
    ]
    assert wanted.progress_revision > revision
    assert wanted.can_earn(current)
    assert client._inventory_service.get_active_campaign(current) is wanted
    assert all(wanted.timed_drops[key].real_current_minutes == 0 for key in ("left", "right"))
    assert unwanted.timed_drops["other"].current_minutes == 0
    client.request_games_update.assert_called_once_with()
    client.change_state.assert_not_called()
    client.drop_history.record.assert_not_called()


@pytest.fixture
def response_mining_pair(mining_pair):
    source, wanted, unwanted, current = mining_pair
    client = object.__new__(Twitch)
    for key in ("settings", "gui", "inventory", "wanted_games", "watching_channel", "_drops", "is_mining_time"):
        setattr(client, key, getattr(source, key))
    client.close = MagicMock()
    client.print = MagicMock()
    client.drop_history = MagicMock()
    client.change_state = MagicMock()
    client.request_games_update = MagicMock()
    client._inventory_service = InventoryService(client)
    current._twitch = client
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    return client, wanted, unwanted, current


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [
    "root_null", "root_list", "user_null", "missing_minutes", "boolean_minutes",
    "session_list", "partial_error", "invalid_errors",
])
@pytest.mark.parametrize("capped", [False, True])
@pytest.mark.parametrize("websocket", [False, True])
async def test_unusable_reply_preserves_state_and_next_poll_recovers(
    response_mining_pair, monkeypatch, caplog, broken, capped, websocket,
):
    client, wanted, unwanted, current = response_mining_pair
    eligible = [wanted.timed_drops[key] for key in ("base", "parallel")]
    for drop in eligible:
        drop.real_current_minutes = 10
        drop.extra_current_minutes = MAX_EXTRA_MINUTES if capped else 2
    session = {"dropID": "parallel", "currentMinutesWatched": 12}
    reply = {"data": {"currentUser": {"dropCurrentSession": session}}}
    if broken == "root_null":
        reply = None
    elif broken == "root_list":
        reply = []
    elif broken == "user_null":
        reply["data"]["currentUser"] = None
    elif broken == "missing_minutes":
        del session["currentMinutesWatched"]
    elif broken == "boolean_minutes":
        session["currentMinutesWatched"] = True
    elif broken == "session_list":
        reply["data"]["currentUser"]["dropCurrentSession"] = []
    elif broken == "partial_error":
        reply["errors"] = [{"message": "private-progress-marker"}]
    else:
        reply["errors"] = False

    def state():
        return (
            tuple((drop.real_current_minutes, drop.extra_current_minutes) for drop in wanted.drops),
            wanted.progress_revision, client.request_games_update.call_count,
            client.gui.inv.update_drop.call_count,
        )

    requests, after_observation, clock = [], [], [0]

    async def query(operation):
        requests.append(clock[0])
        if len(requests) == 1:
            if websocket:
                await MessageHandlerService(client).process_drops(1, {"type": "drop-progress", "data": {
                    "drop_id": "parallel", "current_progress_min": 11, "required_progress_min": 200,
                }})
            after_observation.append(state())
            return reply
        assert state() == after_observation[0]
        return {"data": {"currentUser": {"dropCurrentSession": {
            "dropID": "parallel", "currentMinutesWatched": 9,
        }}}}

    client.gql_request = AsyncMock(side_effect=query)
    client.gui.progress.minute_almost_done.return_value = False
    monkeypatch.setattr(Channel, "send_watch", AsyncMock(return_value=True))
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    watcher = WatchService(client)

    async def advance(delay):
        clock[0] += delay
        if clock[0] >= 130:
            raise asyncio.CancelledError

    watcher.watch_sleep = advance
    tasks = asyncio.all_tasks()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(watcher.watch_loop(), 2)
    assert not {task for task in asyncio.all_tasks() - tasks if not task.done()}
    assert requests == [20, 80]
    assert current.send_watch.await_count == 13
    assert [(drop.real_current_minutes, drop.extra_current_minutes) for drop in eligible] == [(9, 0), (9, 0)]
    assert all(wanted.timed_drops[key].current_minutes == 0 for key in ("left", "right"))
    assert unwanted.timed_drops["other"].current_minutes == 0
    assert client.request_games_update.call_count == int(capped)
    client.change_state.assert_not_called()
    client.drop_history.record.assert_not_called()
    client.close.assert_not_called()
    assert "private-progress-marker" not in caplog.text
    assert not any(record.levelname == "ERROR" for record in caplog.records)
