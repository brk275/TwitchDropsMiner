"""Unusable progress responses must not kill watching or manufacture progress."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.gql_client import GQLClient
from src.core.client import Twitch
from src.exceptions import GQLException
from src.models.campaign import DropsCampaign
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_drop_ignore_policy import _drop


def response(session):
    return {"data": {"currentUser": {"dropCurrentSession": session}}}


@pytest.fixture
def mining():
    twitch = object.__new__(Twitch)
    twitch.settings = SimpleNamespace(
        drop_name_blacklist=[], mining_benefits={"DIRECT_ENTITLEMENT": True},
        allow_unlinked_campaigns=False,
    )
    twitch.gui = MagicMock()
    twitch.gui.progress.minute_almost_done.return_value = True
    twitch.close = MagicMock()
    twitch.print = MagicMock()
    twitch.drop_history = MagicMock()
    twitch.is_mining_time = lambda: True
    raw = [_drop(key, key, required_minutes=60) for key in ("drop", "sibling")]
    for item in raw:
        item["self"] = {"dropInstanceID": "test-claim", "isClaimed": False, "currentMinutesWatched": 10}
    campaign = DropsCampaign(twitch, {
        "id": "campaign", "name": "Campaign", "game": {"id": "1", "name": "Game"},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.test/link",
        "startAt": "2000-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
        "status": "ACTIVE", "allow": {"channels": [], "isEnabled": True}, "timeBasedDrops": raw,
    }, {})
    for drop in campaign.drops:
        drop.extra_current_minutes = 2
    channel = MagicMock()
    channel.id, channel.game = 123, campaign.game
    channel.online = channel.drops_enabled = True
    channel.banned = False
    channel._stream = object()
    channel._watch_current.return_value = True
    channel.send_watch = AsyncMock(return_value=True)
    twitch.watching_channel = AwaitableValue()
    twitch.watching_channel.set(channel)
    twitch._drops = dict(campaign.timed_drops)
    twitch.inventory, twitch.wanted_games = [campaign], [campaign.game]
    twitch._inventory_service = SimpleNamespace(get_active_campaign=MagicMock(return_value=campaign))
    return twitch, campaign, channel


def counts(campaign):
    return [(drop.real_current_minutes, drop.extra_current_minutes) for drop in campaign.drops]


async def run_watch(mining, monkeypatch, first, *, second=None, query=None,
                    poll_times=(20, 80), min_watches=8, cleanup=None):
    twitch, campaign, channel = mining
    clock, requests, snapshots, watches = [0.0], [], [], []
    if second is None:
        second = response({"dropID": "drop", "currentMinutesWatched": 9})

    async def ask(operation):
        requests.append(clock[0])
        snapshots.append(counts(campaign))
        if query is not None:
            return await query(operation)
        item = first if len(requests) == 1 else second
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_watch():
        watches.append(clock[0])
        return True

    async def advance(delay):
        clock[0] += delay
        if clock[0] >= 90:
            raise asyncio.CancelledError

    channel.send_watch = send_watch
    twitch.gql_request = ask
    service = WatchService(twitch)
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "watch_sleep", advance)
    tasks = asyncio.all_tasks()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service.watch_loop(), 2)
    finally:
        if cleanup is not None:
            await cleanup()
    assert not {task for task in asyncio.all_tasks() - tasks if not task.done()}
    assert requests == list(poll_times)
    assert len(watches) >= min_watches and all(b - a <= 10 for a, b in zip(watches, watches[1:], strict=False))
    twitch.close.assert_not_called()
    twitch.drop_history.record.assert_not_called()
    return snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [
    pytest.param({"data": {"currentUser": None}}, id="null-user"),
    pytest.param(response({"dropID": "drop"}), id="missing-minutes"),
    pytest.param(None, id="null-result"),
    pytest.param([], id="array-result"),
    pytest.param("not-an-object", id="string-result"),
    pytest.param(0, id="numeric-result"),
    pytest.param(False, id="boolean-result"),
    pytest.param({}, id="missing-data"),
    pytest.param({"data": None}, id="null-data"),
    pytest.param({"data": []}, id="array-data"),
    pytest.param({"data": {}}, id="missing-user"),
    pytest.param({"data": {"currentUser": []}}, id="array-user"),
    pytest.param({"data": {"currentUser": False}}, id="boolean-user"),
    pytest.param({"data": {"currentUser": {}}}, id="missing-session"),
    pytest.param(response([]), id="array-session"),
    pytest.param(response(1), id="numeric-session"),
    pytest.param(response("session"), id="string-session"),
    pytest.param(response({}), id="empty-session"),
    pytest.param(response({"currentMinutesWatched": 9}), id="missing-id"),
    pytest.param(response({"dropID": "", "currentMinutesWatched": 9}), id="empty-id"),
    pytest.param(response({"dropID": None, "currentMinutesWatched": 9}), id="null-id"),
    pytest.param(response({"dropID": [], "currentMinutesWatched": 9}), id="array-id"),
    pytest.param(response({"dropID": {}, "currentMinutesWatched": 9}), id="object-id"),
    pytest.param(response({"dropID": 1, "currentMinutesWatched": 9}), id="numeric-id"),
    pytest.param(response({"dropID": False, "currentMinutesWatched": 9}), id="boolean-id"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": None}), id="null-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": False}), id="false-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": True}), id="true-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": 1.5}), id="fractional-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": float("nan")}), id="nan-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": float("inf")}), id="infinite-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": "9"}), id="string-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": []}), id="array-minutes"),
    pytest.param(response({"dropID": "drop", "currentMinutesWatched": {}}), id="object-minutes"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}),
                  "errors": [{"message": "test server error"}]}, id="partial-error"),
    pytest.param({**response(None), "errors": [{"message": "test server error"}]}, id="errored-empty-session"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}), "errors": False}, id="false-errors"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}), "errors": {}}, id="object-errors"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}), "errors": "error"}, id="string-errors"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}), "errors": 0}, id="numeric-errors"),
    pytest.param({**response({"dropID": "drop", "currentMinutesWatched": 9}), "error": None}, id="singular-error"),
    pytest.param(response({"dropID": "unknown", "currentMinutesWatched": None}), id="malformed-unknown-drop"),
])
async def test_incomplete_response_does_not_stop_watch_loop_and_next_poll_recovers(mining, monkeypatch, broken, caplog):
    snapshots = await run_watch(mining, monkeypatch, broken)
    assert snapshots == [[(10, 2), (10, 2)], [(10, 2), (10, 2)]]
    assert counts(mining[1]) == [(9, 0), (9, 0)]
    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [-1, 0, 9, 10, 60, 99])
async def test_valid_integer_readings_keep_existing_model_clamping(mining, monkeypatch, minutes):
    reply = response({"dropID": "drop", "currentMinutesWatched": minutes})
    await run_watch(mining, monkeypatch, reply, second=reply)
    assert counts(mining[1]) == [(max(0, min(60, minutes)), 0)] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [response(None), GQLException("test query failure"), TimeoutError()])
async def test_explicit_empty_session_and_transport_failures_keep_estimate_fallback(mining, monkeypatch, first):
    snapshots = await run_watch(mining, monkeypatch, first)
    assert snapshots == [[(10, 2), (10, 2)], [(10, 3), (10, 3)]]
    assert counts(mining[1]) == [(9, 0), (9, 0)]


@pytest.mark.asyncio
async def test_valid_unknown_drop_keeps_existing_fallback(mining, monkeypatch):
    reply = response({"dropID": "not-loaded", "currentMinutesWatched": 9})
    await run_watch(mining, monkeypatch, reply, second=reply)
    assert counts(mining[1]) == [(10, 4), (10, 4)]


@pytest.mark.parametrize("errors", [None, []])
def test_parser_allows_extra_metadata_and_no_errors(errors):
    reply = {**response({"dropID": "drop", "currentMinutesWatched": 0, "futureField": "ignored"}),
             "errors": errors, "extensions": {"requestID": "test"}}
    assert WatchService._current_drop_reading(reply) == ("drop", 0)
    assert WatchService._current_drop_reading({**response(None), "errors": errors}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    {"data": {"currentUser": None}},
    {**response(None), "errors": [{"message": "server error", "path": ["currentUser", "dropCurrentSession"]}]},
])
async def test_null_and_partial_error_results_through_real_gql_client_keep_polling(mining, monkeypatch, first):
    class Reply:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return SimpleNamespace(json=AsyncMock(return_value=self.payload))

        async def __aexit__(self, *args):
            return False

    http = SimpleNamespace(request=MagicMock(side_effect=[
        Reply(first), Reply(response({"dropID": "drop", "currentMinutesWatched": 9})),
    ]))
    auth = SimpleNamespace(browser_active=False, headers=MagicMock(return_value={}))
    auth.validate = AsyncMock(return_value=auth)
    client = GQLClient(http, auth, SimpleNamespace(USER_AGENT="test"))

    async def cleanup():
        limiter = client._qgl_limiter
        task, limiter._reset_task = limiter._reset_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    snapshots = await run_watch(mining, monkeypatch, None, query=client.request, cleanup=cleanup)
    assert snapshots == [[(10, 2), (10, 2)], [(10, 2), (10, 2)]]
    assert counts(mining[1]) == [(9, 0), (9, 0)]
    assert http.request.call_count == auth.validate.await_count == 2


@pytest.mark.asyncio
async def test_query_cancellation_propagates_without_closing_client(mining, monkeypatch):
    await run_watch(mining, monkeypatch, asyncio.CancelledError(), poll_times=(20,), min_watches=2)
    assert counts(mining[1]) == [(10, 2), (10, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["query", "model"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_unexpected_failures_still_reach_critical_task_handler(mining, monkeypatch, failure, error_type):
    first = response({"dropID": "drop", "currentMinutesWatched": 9})
    if failure == "query":
        first = error_type("synthetic failure")
    else:
        monkeypatch.setattr(mining[1].timed_drops["drop"], "update_minutes", MagicMock(side_effect=error_type("synthetic failure")))
    with pytest.raises(error_type, match="synthetic failure"):
        await run_watch(mining, monkeypatch, first)
    mining[0].close.assert_called_once_with()
