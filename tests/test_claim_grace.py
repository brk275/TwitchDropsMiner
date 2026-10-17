"""Retained claim IDs should reach Twitch throughout its documented maximum grace."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.exceptions import GQLException
from src.services.stream_selector import StreamSelector
from src.web.managers.inventory import InventoryManager
from tests.test_drop_ignore_policy import _campaign, _drop


END = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@contextmanager
def at(stamp):
    with (
        patch("src.models.drop.datetime", wraps=datetime) as drop_clock,
        patch("src.models.campaign.datetime", wraps=datetime) as campaign_clock,
    ):
        drop_clock.now.return_value = campaign_clock.now.return_value = stamp
        yield


def reward(*, claim_id="claim-reward", claimed=False, campaign_end=END):
    data = _drop("reward", "Reward", ends_at=(END - timedelta(days=2)).isoformat())
    data["startAt"] = (END - timedelta(days=7)).isoformat()
    data["self"] = {
        "isClaimed": claimed, "dropInstanceID": claim_id, "currentMinutesWatched": 15,
    }
    campaign = _campaign([data])
    campaign.starts_at = END - timedelta(days=7)
    campaign.ends_at = campaign_end
    twitch = campaign._twitch
    twitch.settings.telegram_bot_token = ""
    twitch.settings.telegram_chat_id = ""
    twitch.settings.discord_webhook_url = ""
    twitch.gui.broadcast_wanted_items_now = AsyncMock()
    twitch.gql_request = AsyncMock(return_value={
        "data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}},
    })
    return campaign.timed_drops["reward"]


@pytest.mark.parametrize("elapsed,expected", [
    (timedelta(seconds=-1), True), (timedelta(0), True),
    (timedelta(hours=23), True), (timedelta(hours=24), True),
    (timedelta(days=1, microseconds=1), True), (timedelta(days=7), True),
    (timedelta(days=10), True), (timedelta(days=14, microseconds=-1), True),
    (timedelta(days=14), False), (timedelta(days=14, microseconds=1), False),
])
def test_claim_window_uses_fourteen_day_maximum_with_exclusive_end(elapsed, expected):
    drop = reward()
    with at(END + elapsed):
        assert drop.can_claim is expected


def test_claim_window_is_anchored_to_campaign_end_not_individual_reward_end():
    drop = reward()
    with at(END + timedelta(days=13)):
        assert drop.ends_at + timedelta(days=14) < END + timedelta(days=13)
        assert drop.can_claim


def test_claim_deadline_compares_equivalent_timezone_offsets():
    drop = reward(campaign_end=END.astimezone(timezone(timedelta(hours=-4))))
    with at(END + timedelta(days=14, microseconds=-1)):
        assert drop.can_claim
    with at(END + timedelta(days=14)):
        assert not drop.can_claim


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [timedelta(days=3), timedelta(days=14)])
async def test_missing_claim_id_never_sends_a_claim_request(elapsed):
    drop = reward(claim_id=None)
    with at(END + elapsed):
        assert not drop.can_claim
        assert await drop.claim() is False
    drop._twitch.gql_request.assert_not_awaited()
    drop._twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ELIGIBLE_FOR_ALL", "DROP_INSTANCE_ALREADY_CLAIMED"])
async def test_expired_campaign_claim_is_sent_and_success_recorded_only_once(status):
    drop = reward()
    twitch = drop._twitch
    twitch.gql_request.return_value = {"data": {"claimDropRewards": {"status": status}}}
    with at(END + timedelta(days=3)):
        assert drop.campaign.expired
        assert await drop.claim() is True
        assert await drop.claim() is True

    assert drop.is_claimed and drop.current_minutes == drop.required_minutes
    twitch.gql_request.assert_awaited_once()
    assert twitch.gql_request.await_args.args[0]["variables"]["input"] == {"dropInstanceID": "claim-reward"}
    twitch.drop_history.record.assert_called_once_with(drop, drop.campaign)
    twitch.request_games_update.assert_called_once_with()
    twitch.gui.broadcast_wanted_items_now.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"data": {"claimDropRewards": {"status": "DROP_INSTANCE_NOT_ELIGIBLE"}}},
    {"data": {"claimDropRewards": None}},
    {"data": {"errors": [{"message": "Test rejection"}]}},
    {"data": {}},
])
async def test_twitch_rejections_cannot_fabricate_a_successful_claim(response):
    drop = reward()
    twitch = drop._twitch
    twitch.gql_request.return_value = response
    drop.real_current_minutes, drop.extra_current_minutes = 10, 2
    with at(END + timedelta(days=10)):
        assert drop.can_claim
        assert await drop.claim() is False
    twitch.gql_request.assert_awaited_once()
    assert not drop.is_claimed
    assert (drop.real_current_minutes, drop.extra_current_minutes) == (10, 2)
    twitch.drop_history.record.assert_not_called()
    twitch.request_games_update.assert_not_called()
    twitch.gui.broadcast_wanted_items_now.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_transport_cannot_fabricate_a_successful_claim():
    drop = reward()
    drop._twitch.gql_request.side_effect = GQLException("Test transport rejection")
    with at(END + timedelta(days=3)):
        assert await drop.claim() is False
    drop._twitch.gql_request.assert_awaited_once()
    assert not drop.is_claimed
    drop._twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
async def test_attempts_stop_at_the_documented_maximum():
    drop = reward()
    with at(END + timedelta(days=14)):
        assert await drop.claim() is False
    drop._twitch.gql_request.assert_not_awaited()
    drop._twitch.drop_history.record.assert_not_called()


@pytest.mark.asyncio
async def test_already_claimed_reward_remains_idempotent_after_the_window():
    drop = reward(claimed=True)
    with at(END + timedelta(days=20)):
        assert not drop.can_claim
        assert await drop.claim() is True
    drop._twitch.gql_request.assert_not_awaited()
    drop._twitch.drop_history.record.assert_not_called()
    drop._twitch.request_games_update.assert_not_called()


def test_extended_claim_attempts_do_not_reopen_expired_mining_or_queue_entries():
    drop = reward()
    settings = drop._twitch.settings
    settings.games_to_watch = [drop.campaign.game.name]
    settings.mining_benefits = {"DIRECT_ENTITLEMENT": True}
    settings.priority_list_only = False
    with at(END + timedelta(days=3)):
        assert drop.can_claim
        assert not drop.can_earn()
        assert not drop.campaign.can_earn()
        assert StreamSelector().get_wanted_games(settings, [drop.campaign]) == []
        serialized = InventoryManager._serialize_drop(drop)
        assert serialized["can_claim"] is True
        assert serialized["is_claimed"] is False
