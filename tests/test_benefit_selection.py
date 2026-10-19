"""Mining preference branches must not restrict Twitch progress or claims."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import MAX_CHANNELS, MAX_EXTRA_MINUTES
from src.drop_history import DropHistory
from src.models.channel import Channel
from src.services.channel_service import ChannelService
from src.services.inventory_service import InventoryService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils.async_helpers import AwaitableValue
from tests.test_priority_fallback import _campaign, _channel, _drop, _settings
from tests.test_session_lifecycle import miner


def scenario(*, strict=False, shared=False, prerequisite=False, starts_in=30, special=False):
    now = datetime.now(timezone.utc)
    starts = now + timedelta(minutes=starts_in)
    settings = _settings(
        "Priority", priority_list_only=strict,
        mining_benefits={"DIRECT_ENTITLEMENT": True, "BADGE": False},
    )
    disabled = _drop("disabled", benefit_type="BADGE")
    wanted = _drop("wanted", starts_at=starts.isoformat(),
                   precondition_drop_ids=["disabled"] if prerequisite else [])
    options = {"settings": settings, "game_id": 509663 if special else 1,
               "acl": (100,) if special else ()}
    if shared:
        future = _campaign("mixed", "Priority", drops=[disabled, wanted], **options)
        campaigns = [future]
    else:
        future = _campaign("future", "Priority", starts_at=starts, drops=[wanted], **options)
        campaigns = [future, _campaign("disabled", "Priority", drops=[disabled], **options)]
    fallback = _campaign("fallback", "Fallback", game_id=2, settings=settings)
    owner = future._twitch
    owner.inventory = campaigns + [fallback]
    for campaign in owner.inventory:
        campaign._twitch = owner
        for drop in campaign.drops:
            drop._twitch = owner
    selector = StreamSelector()
    owner.wanted_games = selector.get_wanted_games(settings, owner.inventory)
    owner._channel_service = ChannelService(owner)
    target = _channel(owner, 100, {"id": "99" if special else "1", "name": "Other" if special else "Priority"},
                      drops_enabled=not special)
    alternate = _channel(owner, 200, {"id": "2", "name": "Fallback"})
    owner.watching_channel.get_with_default.return_value = alternate
    return owner, target, alternate, future, selector


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("special", [False, True])
def test_future_target_does_not_authorize_unrelated_disabled_work(strict, shared, special):
    owner, target, alternate, _, selector = scenario(strict=strict, shared=shared, special=special)
    tree = selector.get_wanted_game_tree(owner.settings, owner.inventory)
    assert tree[0]["game_name"] == "Priority"
    assert [d["name"] for c in tree[0]["campaigns"] for d in c["drops"]] == ["wanted"]
    watcher = WatchService(owner)
    assert not watcher.can_watch(target)
    assert not (watcher.can_watch(target) and watcher.should_switch(target))
    assert InventoryService(owner).get_active_campaign(target) is None
    assert owner._channel_service.get_mining_priority(target)[0] == 2
    assert watcher.can_watch(alternate) is (not strict)
    if not strict:
        assert owner._channel_service.get_mining_priority(alternate)[0] == 1


@pytest.mark.parametrize("special", [False, True])
@pytest.mark.parametrize("prerequisite", [False, True])
def test_target_horizon_cannot_borrow_timing_from_disabled_sibling(special, prerequisite):
    owner, target, _, campaign, selector = scenario(shared=True, starts_in=120, special=special,
                                                    prerequisite=prerequisite)
    assert campaign.can_earn_within(datetime.now(timezone.utc) + timedelta(hours=1))
    assert not campaign.mining_selection().targets
    assert [g["game_name"] for g in selector.get_wanted_game_tree(owner.settings, owner.inventory)] == ["Fallback"]
    assert not WatchService(owner).can_watch(target)


@pytest.mark.parametrize("special", [False, True])
def test_disabled_prerequisite_is_retained_and_released_after_claim(special):
    owner, target, _, campaign, _ = scenario(shared=True, prerequisite=True, special=special)
    selection = campaign.mining_selection()
    assert [d.id for d in selection.targets] == ["wanted"]
    assert [d.id for d in selection.branches] == ["disabled", "wanted"]
    assert WatchService(owner).can_watch(target)
    assert InventoryService(owner).get_active_campaign(target) is campaign
    assert campaign.first_mining_drop(target).id == "disabled"
    campaign.bump_minutes(target)
    disabled = campaign.timed_drops["disabled"]
    assert disabled.extra_current_minutes == 1 and disabled.real_current_minutes == 0
    disabled.is_claimed = True
    assert [d.id for d in campaign.mining_selection().branches] == ["wanted"]
    assert not WatchService(owner).can_watch(target)


def test_preference_and_claim_changes_recompute_selection_without_changing_raw_mineability():
    owner, target, _, campaign, _ = scenario(shared=True)
    raw = campaign.mineable_drop_ids
    assert not campaign.can_mine(target)
    owner.settings.mining_benefits["BADGE"] = True
    assert campaign.can_mine(target)
    owner.settings.mining_benefits["BADGE"] = False
    assert not campaign.can_mine(target)
    assert campaign.mineable_drop_ids == raw
    campaign.timed_drops["wanted"].is_claimed = True
    assert campaign.mining_selection().targets == ()
    assert campaign.can_earn(target)


@pytest.mark.parametrize("gate", ["missing", "cycle", "ignored", "nonwatch", "expired"])
def test_invalid_target_branch_cannot_authorize_disabled_prerequisite(gate):
    settings = _settings("Priority", mining_benefits={"DIRECT_ENTITLEMENT": True, "BADGE": False})
    parents = ["disabled"]
    if gate in ("missing", "cycle"):
        parents.append("missing" if gate == "missing" else "wanted")
    if gate == "ignored":
        settings.drop_name_blacklist = ["wanted"]
    campaign = _campaign("mixed", "Priority", settings=settings, drops=[
        _drop("disabled", benefit_type="BADGE"),
        _drop("wanted", precondition_drop_ids=parents, required_minutes=0 if gate == "nonwatch" else 30,
              ends_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat() if gate == "expired" else "2099-01-01T00:00:00Z"),
    ])
    assert not campaign.mining_selection().targets
    assert campaign.can_earn(_channel(campaign._twitch, 100, {"id": "1", "name": "Priority"}))


def test_mixed_campaign_estimates_and_predisplay_choose_only_wanted_work():
    owner, target, _, campaign, _ = scenario(shared=True, starts_in=-1)
    disabled = campaign.timed_drops["disabled"]
    disabled.required_minutes = 1
    assert campaign.first_drop is disabled
    wanted = campaign.timed_drops["wanted"]
    assert campaign.first_mining_drop(target) is wanted
    campaign.bump_minutes(target)
    assert disabled.extra_current_minutes == disabled.real_current_minutes == 0
    assert wanted.extra_current_minutes == 1 and wanted.real_current_minutes == 0
    owner.gui.display_drop.assert_called_once_with(wanted, countdown=True, subone=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_empty_current_session_does_not_estimate_unrelated_disabled_work(monkeypatch, shared):
    owner, target, _, _, _ = scenario(shared=shared)
    owner.watching_channel = AwaitableValue()
    owner.watching_channel.set(target)
    owner.is_mining_time = lambda: True
    owner._drops = {d.id: d for c in owner.inventory for d in c.drops}
    owner._inventory_service = InventoryService(owner)
    owner.gql_request = AsyncMock(return_value={"data": {"currentUser": {"dropCurrentSession": None}}})
    clock = [0.0]
    service = WatchService(owner)

    async def sleep(delay):
        clock[0] += delay
        if clock[0] >= 30:
            raise asyncio.CancelledError

    monkeypatch.setattr(Channel, "send_watch", AsyncMock(return_value=True))
    monkeypatch.setattr("src.services.watch_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "watch_sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(service.watch_loop(), 2)
    disabled = next(d for c in owner.inventory for d in c.drops if d.id == "disabled")
    assert disabled.real_current_minutes == disabled.extra_current_minutes == 0


@pytest.mark.asyncio
async def test_disabled_type_twitch_progress_and_confirmed_claim_remain_accepted(tmp_path):
    owner, target, _, campaign, _ = scenario(shared=True)
    disabled = campaign.timed_drops["disabled"]
    assert disabled.can_reconcile_progress(target)
    disabled.update_minutes(2)
    assert disabled.real_current_minutes == 2
    assert campaign.timed_drops["wanted"].real_current_minutes == 0
    owner.drop_history = DropHistory(tmp_path)
    owner.settings.telegram_bot_token = owner.settings.telegram_chat_id = ""
    owner.settings.discord_webhook_url = ""
    owner.gui.broadcast_wanted_items_now = AsyncMock()
    owner.gql_request = AsyncMock(return_value={"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}})
    disabled.update_claim("synthetic-earned-instance")
    assert await disabled.claim()
    assert disabled.is_claimed and disabled.real_current_minutes == disabled.required_minutes
    assert DropHistory(tmp_path).get_entries()[0]["id"] == disabled.id
    owner.gql_request.assert_awaited_once()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_real_discovery_cap_keeps_useful_work_ahead_of_future_only_game(tmp_path, monkeypatch, strict):
    owner, target, alternate, _, _ = scenario(strict=strict)
    if strict:
        owner.settings.games_to_watch.append("Fallback")
    client = miner(tmp_path, monkeypatch)
    client.settings = owner.settings
    client.inventory = owner.inventory
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client._drops = {d.id: d for c in client.inventory for d in c.drops}
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=type("Identity", (), {"user_id": 42})())
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client.is_mining_time = lambda: True
    candidates = [target] + [_channel(client, i + 1000, {"id": "1", "name": "Priority"}) for i in range(MAX_CHANNELS - 1)]
    client.get_live_streams = AsyncMock(side_effect=lambda game, **kwargs: candidates if game.id == 1 else [alternate])
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(side_effect=lambda channels, **kwargs: list(channels))
    client.gql_request = AsyncMock(side_effect=AssertionError("No live requests"))
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    original_watch = client.watch

    def watch_and_finish(channel, **kwargs):
        original_watch(channel, **kwargs)
        asyncio.get_running_loop().call_soon(client.close)

    client.watch = MagicMock(side_effect=watch_and_finish)
    try:
        await asyncio.wait_for(client._run(), 3)
        displayed = client.gui.channels.batch_update.call_args.args[0]
        assert len(displayed) == MAX_CHANNELS
        assert displayed[0] is alternate
        assert alternate.id in client.channels
        assert client.watching_channel.get_with_default(None) is alternate
        client.gql_request.assert_not_awaited()
        assert client.drop_history.total_count == 0
    finally:
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)


def test_estimate_cap_remains_enforced_without_filtering_raw_reconciliation():
    _, target, _, campaign, _ = scenario(shared=True, starts_in=-1)
    wanted = campaign.timed_drops["wanted"]
    wanted.extra_current_minutes = MAX_EXTRA_MINUTES
    assert not campaign.can_mine(target)
    assert wanted.can_reconcile_progress(target)
    wanted.update_minutes(1)
    assert wanted.extra_current_minutes == 0 and campaign.can_mine(target)


def test_target_after_campaign_end_cannot_authorize_disabled_prerequisite():
    _, target, _, campaign, _ = scenario(shared=True, prerequisite=True)
    campaign.ends_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert campaign.can_earn(target)
    assert campaign.mining_selection().targets == ()
    assert not campaign.can_mine(target)


def test_claimed_expired_prerequisite_does_not_block_an_active_wanted_reward():
    _, target, _, campaign, _ = scenario(shared=True, prerequisite=True, starts_in=-1)
    disabled = campaign.timed_drops["disabled"]
    disabled.ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    disabled.is_claimed = True
    assert [d.id for d in campaign.mining_selection().branches] == ["wanted"]
    assert campaign.can_mine(target)


def test_expired_unclaimed_prerequisite_cannot_justify_wanted_branch():
    _, target, _, campaign, _ = scenario(shared=True, prerequisite=True)
    campaign.timed_drops["disabled"].ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert campaign.mining_selection().targets == ()
    assert not campaign.can_mine(target)


def test_local_guess_remaining_work_excludes_unrelated_disabled_branches():
    owner, target, _, campaign, _ = scenario(shared=True, starts_in=-1)
    campaign.timed_drops["disabled"].required_minutes = 1000
    other = _campaign("other", "Priority", settings=owner.settings,
                      drops=[_drop("other-wanted", required_minutes=30)])
    owner.inventory.append(other)
    assert campaign.remaining_minutes == 1000
    assert campaign.mining_remaining_minutes == 15
    assert InventoryService(owner).get_active_campaign(target) is campaign


def test_horizon_boundary_uses_strict_start_before_stamp(monkeypatch):
    _, _, _, campaign, _ = scenario(shared=True)
    now = datetime.now(timezone.utc)
    stamp = now + timedelta(hours=1)
    wanted = campaign.timed_drops["wanted"]
    wanted.starts_at = stamp
    assert not campaign.mining_selection(stamp=stamp).targets
    wanted.starts_at = stamp - timedelta(microseconds=1)
    assert [d.id for d in campaign.mining_selection(stamp=stamp).targets] == ["wanted"]
