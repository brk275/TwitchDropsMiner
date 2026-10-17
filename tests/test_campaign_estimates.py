"""Estimate-limit detection must not truncate a campaign's minute update."""

from datetime import datetime, timedelta, timezone
from unittest.mock import call

import pytest

from src.config import MAX_EXTRA_MINUTES, State
from tests.test_drop_ignore_policy import _campaign, _drop
from tests.test_special_game_watch import _channel


@pytest.mark.parametrize("limited", [(), (0,), (1,), (2,), (0, 1), (0, 1, 2)])
def test_all_eligible_drops_update_before_requesting_channel_switch(limited, caplog):
    campaign = _campaign([
        _drop(str(index), f"Reward {index}", required_minutes=100)
        for index in range(3)
    ])
    channel = _channel(campaign._twitch)
    drops = list(campaign.drops)
    for index, drop in enumerate(drops):
        drop.real_current_minutes = 5
        drop.extra_current_minutes = MAX_EXTRA_MINUTES - 1 if index in limited else 0
        assert drop.can_earn(channel)

    expected = [MAX_EXTRA_MINUTES if index in limited else 1 for index in range(3)]
    snapshots = []
    campaign._twitch.change_state.side_effect = lambda state: snapshots.append(
        [drop.extra_current_minutes for drop in drops]
    )

    campaign.bump_minutes(channel)

    assert [drop.real_current_minutes for drop in drops] == [5, 5, 5]
    assert [drop.extra_current_minutes for drop in drops] == expected
    assert campaign._twitch.gui.inv.update_drop.call_args_list == [call(drop) for drop in drops]
    if limited:
        campaign._twitch.change_state.assert_called_once_with(State.CHANNEL_SWITCH)
        assert snapshots == [expected]
        assert len(caplog.records) == 1
        assert "maximum extra minutes limit" in caplog.records[0].message
    else:
        campaign._twitch.change_state.assert_not_called()
        assert not caplog.records

    earnable = [drop for drop in drops if drop.can_earn(channel)]
    if earnable:
        campaign._twitch.gui.display_drop.assert_called_once_with(
            min(earnable, key=lambda drop: drop.remaining_minutes), countdown=True, subone=False,
        )
    else:
        campaign._twitch.gui.display_drop.assert_not_called()


@pytest.mark.parametrize(
    "blocker", ["claimed", "ignored", "expired", "upcoming", "prerequisite", "non_watch", "capped"],
)
def test_limit_detection_preserves_other_drop_guards(blocker):
    campaign = _campaign([
        _drop("limit", "Limit reward", required_minutes=100),
        _drop("blocked", "Blocked reward", required_minutes=100),
        _drop("eligible", "Eligible reward", required_minutes=100),
    ])
    channel = _channel(campaign._twitch)
    limited, blocked, eligible = campaign.drops
    limited.extra_current_minutes = MAX_EXTRA_MINUTES - 1
    now = datetime.now(timezone.utc)
    if blocker == "claimed":
        blocked.is_claimed = True
    elif blocker == "ignored":
        campaign._twitch.settings.drop_name_blacklist = [blocked.name]
    elif blocker == "expired":
        blocked.ends_at = now - timedelta(seconds=1)
    elif blocker == "upcoming":
        blocked.starts_at = now + timedelta(hours=1)
    elif blocker == "prerequisite":
        blocked.precondition_drops = ["missing"]
    elif blocker == "non_watch":
        blocked.required_minutes = 0
    elif blocker == "capped":
        blocked.extra_current_minutes = MAX_EXTRA_MINUTES
    assert not blocked.can_earn(channel)
    before = blocked.extra_current_minutes

    campaign.bump_minutes(channel)

    assert limited.extra_current_minutes == MAX_EXTRA_MINUTES
    assert blocked.extra_current_minutes == before
    assert eligible.extra_current_minutes == 1
    assert campaign._twitch.gui.inv.update_drop.call_args_list == [call(limited), call(eligible)]
    campaign._twitch.change_state.assert_called_once_with(State.CHANNEL_SWITCH)


def test_capped_drops_are_not_incremented_again_on_a_later_call():
    campaign = _campaign([
        _drop("capped", "Capped reward", required_minutes=100),
        _drop("eligible", "Eligible reward", required_minutes=100),
    ])
    channel = _channel(campaign._twitch)
    capped, eligible = campaign.drops
    capped.extra_current_minutes = MAX_EXTRA_MINUTES - 1

    campaign.bump_minutes(channel)
    campaign.bump_minutes(channel)

    assert capped.extra_current_minutes == MAX_EXTRA_MINUTES
    assert eligible.extra_current_minutes == 2
    assert [drop.real_current_minutes for drop in campaign.drops] == [0, 0]
    campaign._twitch.change_state.assert_called_once_with(State.CHANNEL_SWITCH)


def test_empty_campaign_has_no_estimate_or_channel_switch():
    campaign = _campaign([])

    campaign.bump_minutes(_channel(campaign._twitch))

    campaign._twitch.change_state.assert_not_called()
    campaign._twitch.gui.inv.update_drop.assert_not_called()
    campaign._twitch.gui.display_drop.assert_not_called()


@pytest.mark.parametrize("blocker", ["unlinked", "expired", "upcoming", "wrong_category", "not_in_acl"])
def test_campaign_or_channel_ineligibility_does_not_create_estimates(blocker):
    campaign = _campaign([
        _drop(str(index), f"Reward {index}", required_minutes=100)
        for index in range(3)
    ])
    channel = _channel(campaign._twitch)
    now = datetime.now(timezone.utc)
    if blocker == "unlinked":
        campaign.linked = False
    elif blocker == "expired":
        campaign.ends_at = now - timedelta(seconds=1)
    elif blocker == "upcoming":
        campaign.starts_at = now + timedelta(hours=1)
    elif blocker == "wrong_category":
        channel = _channel(campaign._twitch, {"id": "2", "name": "Other Game"})
    elif blocker == "not_in_acl":
        campaign.allowed_channels = [_channel(campaign._twitch, id=999)]
    for drop in campaign.drops:
        drop.extra_current_minutes = MAX_EXTRA_MINUTES - 1
        assert not drop.can_earn(channel)

    campaign.bump_minutes(channel)

    assert all(drop.extra_current_minutes == MAX_EXTRA_MINUTES - 1 for drop in campaign.drops)
    assert all(drop.real_current_minutes == 0 for drop in campaign.drops)
    campaign._twitch.change_state.assert_not_called()
    campaign._twitch.gui.inv.update_drop.assert_not_called()
