"""Only usable, error-free claim confirmations may change local claim state."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.auth.session_bundle import SessionError
from src.config import GQL_OPERATIONS
from src.exceptions import ExitRequest, GQLException, LoginException
from src.models.drop import BaseDrop, TimedDrop
from tests.test_imported_session_retry import running_miner
from tests.test_watch_drop_filtering import _campaign, _drop


@pytest.fixture(params=[BaseDrop, TimedDrop], ids=["base", "timed"])
def reward(request):
    data = _drop("reward", "Watch", 30)
    campaign = _campaign("claim-shapes", [data])
    drop = (campaign.timed_drops["reward"] if request.param is TimedDrop
            else BaseDrop(campaign, data, {}))
    drop.update_claim("test-claim-id")
    if isinstance(drop, TimedDrop):
        drop.real_current_minutes, drop.extra_current_minutes = 8, 2
    twitch = campaign._twitch
    twitch.gql_request = AsyncMock()
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    drop._send_telegram_notification = AsyncMock()
    drop._send_discord_notification = AsyncMock()
    return drop, twitch


def state(drop):
    return (drop.is_claimed, drop.claim_id,
            (drop.real_current_minutes, drop.extra_current_minutes) if isinstance(drop, TimedDrop) else None)


BAD = [None, False, 7, "private reply", [], [{}], {},
       *({"data": value} for value in (None, False, 7, "private data", [], {})),
       *({"data": {"claimDropRewards": value}} for value in (None, False, True, 7, "private claim", [], [1], {}, {"other": 1})),
       *({"data": {"claimDropRewards": {"status": value}}}
         for value in (None, False, 7, [], {}, "", "NOT_ELIGIBLE")),
       {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}, "errors": [{"message": "test error"}]}]


async def assert_unconfirmed(reward, payload):
    drop, twitch = reward
    before, original = state(drop), deepcopy(payload)
    twitch.gql_request.return_value = payload
    assert await drop.claim() is False
    assert state(drop) == before
    assert payload == original
    twitch.gql_request.assert_awaited_once_with(
        GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": "test-claim-id"}}))
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    drop._send_telegram_notification.assert_not_awaited()
    drop._send_discord_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", BAD)
async def test_unusable_claim_reply_returns_false_without_success_effects(reward, payload):
    await assert_unconfirmed(reward, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"])
@pytest.mark.parametrize("empty_errors", ["absent", "root_null", "root_empty", "data_null", "data_empty"])
async def test_accepted_claim_status_records_one_success_without_an_extra_request(reward, status, empty_errors):
    drop, twitch = reward
    payload = {"data": {"claimDropRewards": {"status": status}}}
    if empty_errors != "absent":
        location = payload if empty_errors.startswith("root") else payload["data"]
        location["errors"] = None if empty_errors.endswith("null") else []
    twitch.gql_request.return_value = payload
    assert await drop.claim() is True
    assert await drop.claim() is True
    assert drop.is_claimed
    twitch.gql_request.assert_awaited_once()
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()
    if isinstance(drop, TimedDrop):
        assert (drop.real_current_minutes, drop.extra_current_minutes) == (30, 0)
        twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    else:
        twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["root", "data"])
@pytest.mark.parametrize("errors", [False, True, 0, "", {}, {"message": "test"}, [None], [{"message": "test"}]])
async def test_errored_or_malformed_metadata_cannot_confirm_a_success_status(reward, where, errors):
    drop, twitch = reward
    payload = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    (payload if where == "root" else payload["data"])["errors"] = errors
    await assert_unconfirmed(reward, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "private failure"])
async def test_legacy_root_error_is_not_a_claim_confirmation(reward, value):
    payload = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}, "error": value}
    await assert_unconfirmed(reward, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GQLException("test"), asyncio.CancelledError(), ExitRequest(),
                                  SessionError("STALE"), LoginException("test"), TimeoutError("test"), RuntimeError("test")])
async def test_only_existing_gql_failure_is_caught_without_replaying(reward, error):
    drop, twitch = reward
    before = state(drop)
    twitch.gql_request.side_effect = error
    if isinstance(error, GQLException):
        assert await drop.claim() is False
    else:
        with pytest.raises(type(error)) as caught:
            await drop.claim()
        assert caught.value is error
    assert state(drop) == before
    twitch.gql_request.assert_awaited_once()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    drop._send_telegram_notification.assert_not_awaited()
    drop._send_discord_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["claimed", "no_id", "expired"])
async def test_claim_early_returns_do_not_send_requests(reward, kind):
    drop, twitch = reward
    if kind == "claimed":
        drop.is_claimed = True
    elif kind == "no_id":
        drop.claim_id = None
    else:
        drop.campaign.ends_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert await drop.claim() is (kind == "claimed")
    twitch.gql_request.assert_not_awaited()
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    drop._send_telegram_notification.assert_not_awaited()
    drop._send_discord_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"data": None}, {"data": {"claimDropRewards": True}},
                                    {"data": {"claimDropRewards": {"status": "unknown"}}}])
async def test_later_explicit_attempt_can_reconcile_a_previously_unconfirmed_claim(reward, payload):
    drop, twitch = reward
    before = state(drop)
    twitch.gql_request.side_effect = [payload, {"data": {"claimDropRewards": {"status": "DROP_INSTANCE_ALREADY_CLAIMED"}}}]
    assert await drop.claim() is False
    assert state(drop) == before
    assert await drop.claim() is True
    assert await drop.claim() is True
    assert twitch.gql_request.await_count == 2
    assert drop.is_claimed
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()
    drop._send_telegram_notification.assert_awaited_once_with()
    drop._send_discord_notification.assert_awaited_once_with()
    if isinstance(drop, TimedDrop):
        twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"data": None}, {"data": False}, {"data": {"claimDropRewards": True}},
                                    {"data": {"claimDropRewards": {}}},
                                    {"data": {"claimDropRewards": {"other": 1}}},
                                    {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}, "nullable": "test"},
                                     "errors": [{"message": "server error", "path": ["nullable"]}]}])
async def test_actual_imported_gql_dispatch_does_not_confirm_bad_operation_data(reward, tmp_path, monkeypatch, payload):
    drop, twitch = reward
    before = state(drop)
    async with running_miner(tmp_path, monkeypatch) as (client, peer, _):
        peer.responses.extend([deepcopy(payload), {"data": {"mustNotRun": True}}])
        twitch.gql_request = client.gql_request
        assert await drop.claim() is False
        operation = GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": drop.claim_id}})
        assert peer.calls == [operation]
        assert len(peer.responses) == 1
        assert client._browser.status()["state"] == "ready"
        assert client._browser.status()["generation"] == 1
    assert state(drop) == before
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()
    drop._send_telegram_notification.assert_not_awaited()
    drop._send_discord_notification.assert_not_awaited()
