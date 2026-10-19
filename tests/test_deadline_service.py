"""Deadline detours preserve ranking, real progress and normal claim admission."""

from datetime import datetime, timedelta, timezone

import pytest

from src.services.channel_service import ChannelService
from src.services.deadline_service import DeadlineService
from src.services.watch_service import WatchService
from tests.test_priority_fallback import _campaign, _channel, _drop, _settings


def scenario(*, high_minutes=240, low_minutes=120, low_end=180, high_end=2880):
    now = datetime.now(timezone.utc)
    settings = _settings("High", "Low", priority_list_only=True)
    high = _campaign("high", "High", game_id=1, settings=settings,
                     ends_at=now + timedelta(minutes=high_end),
                     drops=[_drop("high-drop", required_minutes=high_minutes)])
    low = _campaign("low", "Low", game_id=2, settings=settings,
                    ends_at=now + timedelta(minutes=low_end),
                    drops=[_drop("low-drop", required_minutes=low_minutes)])
    owner = high._twitch
    owner.inventory = [high, low]
    owner.wanted_games = [high.game, low.game]
    owner.is_manual_mode.return_value = False
    low._twitch = owner
    for drop in low.drops:
        drop._twitch = owner
    channels = [_channel(owner, i, {"id": str(i), "name": name})
                for i, name in [(1, "High"), (2, "Low")]]
    service = DeadlineService(owner, enabled=True)
    owner._deadline_service = service
    owner._channel_service = ChannelService(owner)
    owner.watching_channel.get_with_default.return_value = channels[0]
    return now, owner, service, high, low, channels


def test_lower_ranked_urgent_reward_preempts_then_returns_after_confirmed_claim():
    now, owner, service, high, low, channels = scenario()
    ranks = list(owner.settings.games_to_watch)
    service.prepare(channels, now=now)
    assert service.target.drop is low.timed_drops["low-drop"]
    assert WatchService(owner).should_switch(channels[1])
    assert sorted(channels, key=owner._channel_service.get_mining_priority)[0] is channels[1]
    assert owner.settings.games_to_watch == ranks
    assert owner.wanted_games == [high.game, low.game]
    owner.watching_channel.get_with_default.return_value = channels[1]
    low.timed_drops["low-drop"].is_claimed = True
    service.prepare(channels, now=now)
    assert service.target is None
    assert WatchService(owner).should_switch(channels[0])


@pytest.mark.parametrize("change", ["disabled", "not-soon", "impossible", "protect-high",
                                   "offline", "banned", "flag", "unlinked", "ignored",
                                   "unwanted", "manual", "hours", "removed", "fallback"])
def test_no_detour_when_unnecessary_or_unsafe(change):
    options = {
        "not-soon": {"low_end": 2880}, "impossible": {"low_end": 120},
        "protect-high": {"high_end": 300},
    }.get(change, {})
    now, owner, service, _, low, channels = scenario(**options)
    if change == "disabled":
        service.enabled = False
    elif change == "offline":
        channels[1]._stream = None
    elif change == "banned":
        channels[1].update_ban_status({"data": {"user": {"id": "2", "self": {
            "banStatus": {"isPermanent": True},
        }}}})
        assert channels[1].banned
    elif change == "flag":
        channels[1]._stream.drops_enabled = False
    elif change == "unlinked":
        low.linked = False
    elif change == "ignored":
        owner.settings.drop_name_blacklist = ["low-drop"]
    elif change == "unwanted":
        owner.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
    elif change == "manual":
        owner.is_manual_mode.return_value = True
    elif change == "hours":
        owner.settings.mining_hours = {"mode": "range"}
    elif change == "removed":
        owner.wanted_games.pop()
    elif change == "fallback":
        owner.settings.games_to_watch = ["High"]
    service.prepare(channels, now=now)
    assert service.target is None


def test_local_timer_cannot_make_impossible_reward_look_finishable():
    now, _, service, _, low, channels = scenario(low_minutes=180, low_end=170)
    low.timed_drops["low-drop"].extra_current_minutes = 30
    service.prepare(channels, now=now)
    assert service.target is None
    low.timed_drops["low-drop"].real_current_minutes = 60
    low.timed_drops["low-drop"].extra_current_minutes = 0
    service.prepare(channels, now=now)
    assert service.target is not None


def test_detour_stays_until_claim_and_releases_on_expiry_or_channel_loss():
    now, _, service, _, low, channels = scenario()
    service.prepare(channels, now=now)
    low.timed_drops["low-drop"].real_current_minutes = 120
    service.prepare(channels, now=now + timedelta(minutes=120))
    assert service.target is not None
    service.prepare(channels[:1], now=now + timedelta(minutes=121))
    assert service.target is None
    service.prepare(channels, now=now + timedelta(minutes=181))
    assert service.target is None


@pytest.mark.parametrize("bad", ["missing", "cycle", "expired", "not-watch", "ignored"])
def test_prerequisite_budget_rejects_invalid_branches(bad):
    now, owner, service, _, low, channels = scenario()
    parent = _drop("parent", required_minutes=0 if bad == "not-watch" else 90)
    replacement = _campaign("low", "Low", game_id=2, settings=owner.settings,
                            ends_at=low.ends_at, drops=[parent, _drop(
                                "low-drop", required_minutes=60,
                                precondition_drop_ids=["missing" if bad == "missing" else "parent"],
                            )])
    replacement._twitch = owner
    owner.inventory[1] = replacement
    node = replacement.timed_drops["parent"]
    if bad == "cycle":
        node.precondition_drops = ["low-drop"]
    elif bad == "expired":
        node.ends_at = now - timedelta(seconds=1)
    elif bad == "ignored":
        owner.settings.drop_name_blacklist = ["parent"]
    service.prepare(channels, now=now)
    # The parent can itself be wanted; it must not certify the invalid child.
    assert service.target is None or service.target.drop.id != "low-drop"
    assert service.finish_at(replacement.timed_drops["low-drop"], now) is None


def test_prerequisite_cost_and_individual_windows_and_shared_deduplication():
    now, owner, service, _, low, _ = scenario()
    campaign = _campaign("chain", "Low", game_id=2, settings=owner.settings, drops=[
        _drop("root", required_minutes=20),
        _drop("left", required_minutes=20, precondition_drop_ids=["root"]),
        _drop("right", required_minutes=20, precondition_drop_ids=["root"]),
        _drop("target", required_minutes=20, precondition_drop_ids=["left", "right"]),
    ])
    target = campaign.timed_drops["target"]
    assert service.finish_at(target, now) == now + timedelta(minutes=80)
    campaign.timed_drops["root"].ends_at = now + timedelta(minutes=30)
    assert service.finish_at(target, now) is None
    campaign.timed_drops["root"].is_claimed = True
    assert service.finish_at(target, now) == now + timedelta(minutes=60)


def test_earliest_deadline_wins_with_stable_rank_tie_break():
    now, owner, service, _, _, channels = scenario()
    third = _campaign("third", "Third", game_id=3, settings=owner.settings,
                      ends_at=now + timedelta(minutes=150), drops=[_drop("third", required_minutes=20)])
    owner.settings.games_to_watch.append("Third")
    owner.inventory.append(third)
    owner.wanted_games.append(third.game)
    channels.append(_channel(owner, 3, {"id": "3", "name": "Third"}))
    service.prepare(channels, now=now)
    # Third can finish, but must not cost Low its own deadline.
    assert service.target.campaign is third
    third.ends_at = now + timedelta(minutes=180)
    service.prepare(channels, now=now)
    assert service.target.campaign.id == "low"


def test_discovery_is_bounded_and_clear_drops_old_account_target(monkeypatch):
    clock = [0]
    monkeypatch.setattr("src.services.deadline_service.monotonic", lambda: clock[0])
    now, _, service, _, _, channels = scenario()
    assert not service.discovery_due()
    clock[0] = 300
    assert service.discovery_due()
    assert not service.discovery_due()
    service.prepare(channels, now=now)
    assert service.target is not None
    service.clear()
    assert service.target is None


@pytest.mark.parametrize("mode", ["manual", "hours", "disabled"])
def test_prepared_priority_is_suppressed_immediately_after_mode_change(mode):
    now, owner, service, _, _, channels = scenario()
    service.prepare(channels, now=now)
    assert service.prioritizes(channels[1])
    if mode == "manual":
        owner.is_manual_mode.return_value = True
    elif mode == "hours":
        owner.settings.mining_hours = {"mode": "range"}
    else:
        service.enabled = False
    assert not service.prioritizes(channels[1])
    assert owner._channel_service.get_mining_priority(channels[1])[0] == 0


def test_unfinishable_selected_game_still_blocks_fallback_deadline_preemption():
    now, owner, service, high, low, channels = scenario(high_end=200)
    owner.settings.games_to_watch = ["High"]
    owner.settings.priority_list_only = False
    fallback = _campaign("fallback", "Fallback", game_id=3, settings=owner.settings,
                         ends_at=now + timedelta(minutes=420), drops=[_drop("fallback", required_minutes=240)])
    owner.inventory.insert(1, fallback)
    owner.wanted_games = [high.game, fallback.game, low.game]
    channels.append(_channel(owner, 3, {"id": "3", "name": "Fallback"}))
    service.prepare(channels, now=now)
    assert high.can_mine(channels[0])
    assert service.target is None


def test_insufficient_cap_for_protected_channels_falls_back_to_normal_rank():
    now, owner, service, _, _, channels = scenario()
    service.prepare(channels, now=now)
    ordered = sorted(channels, key=owner._channel_service.get_mining_priority)
    assert service.cap_channels(ordered, 1) == [channels[0]]
    assert service.target is None


@pytest.mark.parametrize("change", ["campaign", "drop", "claim", "ignore", "benefits"])
def test_stale_detour_cannot_drive_callback_switch_after_inventory_or_policy_change(change):
    now, owner, service, _, low, channels = scenario()
    service.prepare(channels, now=now)
    assert WatchService(owner).should_switch(channels[1])
    replacement = _campaign("low", "Low", game_id=2, settings=owner.settings,
                            ends_at=low.ends_at, drops=[_drop("low-drop", required_minutes=240)])
    if change == "campaign":
        owner.inventory[1] = replacement
    elif change == "drop":
        low.timed_drops = replacement.timed_drops
    elif change == "claim":
        low.timed_drops["low-drop"].is_claimed = True
    elif change == "ignore":
        owner.settings.drop_name_blacklist = ["low-drop"]
    else:
        owner.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
    # No prepare() here: channel callbacks can run during GUI publication.
    assert not service.prioritizes(channels[1])
    assert owner._channel_service.get_mining_priority(channels[1])[0] != -1
    if change in ("campaign", "drop"):
        assert not WatchService(owner).should_switch(channels[1])
