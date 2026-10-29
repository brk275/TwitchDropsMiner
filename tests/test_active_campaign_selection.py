"""Active campaign lookup returns eligible local candidates, not Twitch allocation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config.reward_priority import RewardPriority
from src.models.game import Game
from src.services.game_order import TemporaryGameOrder
from src.services.inventory_service import InventoryService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils import AwaitableValue
from tests.test_priority_fallback import _campaign, _channel, _drop, _settings


@pytest.fixture(params=['off', 'reward', 'temporary', 'both'])
def mode(request):
    return request.param


def client(mode, *games, priority_list_only=True):
    settings = _settings(
        *games, priority_list_only=priority_list_only,
        reward_priority=list(RewardPriority.TYPES) if mode in ('reward', 'both') else [],
        mining_benefits=dict.fromkeys(RewardPriority.TYPES, True),
    )
    order = TemporaryGameOrder()
    if mode in ('temporary', 'both'):
        order.set(list(games))
    return SimpleNamespace(
        settings=settings, inventory=[], wanted_games=[], game_order=order,
        watching_channel=AwaitableValue(), gui=MagicMock(),
    )


def add_campaign(twitch, key, game, game_id, minutes=30, *, acl=(), **kwargs):
    drops = kwargs.pop('drops', [_drop(key + '-drop', required_minutes=minutes)])
    campaign = _campaign(
        key, game, game_id=game_id, settings=twitch.settings, drops=drops, acl=acl, **kwargs,
    )
    campaign._twitch = twitch
    for drop in campaign.drops:
        drop._twitch = twitch
    twitch.inventory.append(campaign)
    return campaign


def queue(twitch):
    twitch.wanted_games = StreamSelector().get_wanted_games(
        twitch.settings, twitch.inventory, twitch.game_order,
    )


@pytest.mark.parametrize('special_id', [509663, 509672])
@pytest.mark.parametrize('want_special', [False, True])
def test_unwanted_shared_campaign_cannot_preempt_wanted_candidate(mode, special_id, want_special):
    twitch = client(mode, 'Special' if want_special else 'Regular')
    regular = add_campaign(twitch, 'regular', 'Regular', 1, 15 if want_special else 120)
    special = add_campaign(twitch, 'special', 'Special', special_id,
                           120 if want_special else 15, acl=(100,))
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    twitch.watching_channel.set(channel)
    wanted = special if want_special else regular
    assert twitch.wanted_games == [wanted.game]
    assert regular.can_earn(channel) and special.can_earn(channel)
    assert WatchService(twitch).can_watch(channel)

    assert InventoryService(twitch).get_active_campaign() is wanted


@pytest.mark.parametrize('context', ['different-id', 'same-id-new-stream', 'no-watcher'])
def test_explicit_channel_uses_its_own_stream_context(mode, context):
    twitch = client(mode, 'Alpha', 'Beta')
    alpha = add_campaign(twitch, 'alpha', 'Alpha', 1)
    beta = add_campaign(twitch, 'beta', 'Beta', 2)
    queue(twitch)
    watched = _channel(twitch, 100, {'id': '1', 'name': 'Alpha'})
    requested = _channel(twitch, 100 if context == 'same-id-new-stream' else 200,
                         {'id': '2', 'name': 'Beta'})
    if context != 'no-watcher':
        twitch.watching_channel.set(watched)
    service = InventoryService(twitch)
    assert service.get_active_campaign() is (alpha if context != 'no-watcher' else None)
    assert beta.can_earn(requested) and not beta.can_earn(watched)

    assert service.get_active_campaign(requested) is beta
    assert twitch.watching_channel.get_with_default(None) is (
        watched if context != 'no-watcher' else None
    )


def test_explicit_channel_without_eligible_campaign_never_uses_watched_channel(mode):
    twitch = client(mode, 'Alpha')
    alpha = add_campaign(twitch, 'alpha', 'Alpha', 1)
    queue(twitch)
    watched = _channel(twitch, 100, {'id': '1', 'name': 'Alpha'})
    requested = _channel(twitch, 200, {'id': '2', 'name': 'Other'})
    twitch.watching_channel.set(watched)
    assert alpha.can_earn(watched) and not alpha.can_earn(requested)

    assert InventoryService(twitch).get_active_campaign(requested) is None
    assert twitch.watching_channel.get_with_default(None) is watched


def test_explicit_channel_does_not_read_watched_value(mode):
    twitch = client(mode, 'Regular')
    campaign = add_campaign(twitch, 'regular', 'Regular', 1)
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    twitch.watching_channel = MagicMock()
    twitch.watching_channel.get_with_default.side_effect = AssertionError('unexpected watcher read')

    assert InventoryService(twitch).get_active_campaign(channel) is campaign
    twitch.watching_channel.get_with_default.assert_not_called()


def test_missing_channel_empty_wanted_and_omitted_argument_controls(mode):
    twitch = client(mode, 'Regular')
    campaign = add_campaign(twitch, 'regular', 'Regular', 1)
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    service = InventoryService(twitch)
    assert service.get_active_campaign() is None
    assert service.get_active_campaign(None) is None
    assert service.get_active_campaign(channel) is campaign
    assert not twitch.watching_channel.has_value()
    twitch.watching_channel.set(channel)
    assert service.get_active_campaign() is campaign
    assert service.get_active_campaign(None) is campaign
    assert service.get_active_campaign(channel) is campaign
    twitch.wanted_games.clear()
    assert service.get_active_campaign() is None
    assert service.get_active_campaign(channel) is None


@pytest.mark.parametrize('wanted', [None, []], ids=['null', 'empty'])
def test_absent_wanted_queue_does_not_guess_from_earnable_inventory(mode, wanted):
    twitch = client(mode, 'Regular')
    campaign = add_campaign(twitch, 'regular', 'Regular', 1)
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    twitch.watching_channel.set(channel)
    twitch.wanted_games = wanted
    assert campaign.can_earn(channel)
    assert not WatchService(twitch).can_watch(channel)

    service = InventoryService(twitch)
    assert service.get_active_campaign() is None
    assert service.get_active_campaign(channel) is None
    assert twitch.watching_channel.get_with_default(None) is channel


@pytest.mark.parametrize('special_id', [509663, 509672])
def test_regular_temporary_rank_cannot_bypass_missing_drops_flag(mode, special_id):
    twitch = client(mode, 'Regular', 'Special')
    regular = add_campaign(twitch, 'regular', 'Regular', 1, 5)
    special = add_campaign(twitch, 'special', 'Special', special_id, 120, acl=(100,))
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'}, drops_enabled=False)
    assert regular.can_earn(channel) and special.can_earn(channel)
    assert WatchService(twitch).can_watch(channel)

    assert InventoryService(twitch).get_active_campaign(channel) is special


def test_regular_campaign_requires_drops_enabled(mode):
    twitch = client(mode, 'Regular')
    campaign = add_campaign(twitch, 'regular', 'Regular', 1)
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'}, drops_enabled=False)
    assert campaign.can_earn(channel)
    assert not WatchService(twitch).can_watch(channel)

    assert InventoryService(twitch).get_active_campaign(channel) is None


@pytest.mark.parametrize('game_id', [1, 509663, 509672], ids=['regular', 'special-events', 'irl'])
@pytest.mark.parametrize('status', ['live', 'offline', 'banned'])
def test_channel_status_matches_watch_eligibility(mode, game_id, status):
    twitch = client(mode, 'Game')
    campaign = add_campaign(twitch, 'campaign', 'Game', game_id, acl=(100,))
    queue(twitch)
    channel = _channel(twitch, 100, {'id': str(game_id), 'name': 'Game'},
                       online=status != 'offline')
    if status == 'banned':
        channel.update_ban_status({'data': {'user': {
            'id': '100', 'self': {'banStatus': {'isPermanent': True}},
        }}})
    assert campaign.can_earn(channel) is (status != 'offline')
    assert WatchService(twitch).can_watch(channel) is (status == 'live')

    assert InventoryService(twitch).get_active_campaign(channel) is (
        campaign if status == 'live' else None
    )


@pytest.mark.parametrize('blocker', [
    'expired', 'upcoming-drop', 'claimed', 'ignored', 'unlinked', 'prerequisite', 'acl',
])
def test_existing_campaign_and_drop_guards_remain_required(mode, blocker):
    twitch = client(mode, 'Regular')
    campaign = add_campaign(twitch, 'regular', 'Regular', 1)
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    drop = campaign.timed_drops['regular-drop']
    if blocker == 'expired':
        campaign.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif blocker == 'upcoming-drop':
        drop.starts_at = datetime.now(timezone.utc) + timedelta(hours=1)
    elif blocker == 'claimed':
        drop.is_claimed = True
    elif blocker == 'ignored':
        twitch.settings.drop_name_blacklist = ['regular-drop']
    elif blocker == 'unlinked':
        campaign.linked = False
    elif blocker == 'prerequisite':
        drop.precondition_drops = ['missing']
    else:
        campaign.allowed_channels = [_channel(twitch, 200, {'id': '1', 'name': 'Regular'})]
    assert not campaign.can_earn(channel)
    assert not WatchService(twitch).can_watch(channel)

    assert InventoryService(twitch).get_active_campaign(channel) is None


def test_reward_order_and_temporary_override_keep_existing_semantics(mode):
    twitch = client(mode, 'Regular', 'Special')
    regular = add_campaign(twitch, 'regular', 'Regular', 1, 5)
    special = add_campaign(twitch, 'special', 'Special', 509663, acl=(100,), drops=[
        _drop('badge', required_minutes=120, benefit_type='BADGE'),
    ])
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})

    expected = special if mode == 'reward' else regular
    assert InventoryService(twitch).get_active_campaign(channel) is expected


def test_remaining_time_breaks_equal_ranks_without_mutating_state(mode):
    twitch = client(mode, 'Regular')
    end = datetime.now(timezone.utc) + timedelta(hours=6)
    long = add_campaign(twitch, 'long', 'Regular', 1, drops=[
        _drop('long-drop', required_minutes=120, ends_at=end.isoformat()),
    ])
    short = add_campaign(twitch, 'short', 'Regular', 1, drops=[
        _drop('short-drop', required_minutes=15, ends_at=end.isoformat()),
    ])
    queue(twitch)
    channel = _channel(twitch, 100, {'id': '1', 'name': 'Regular'})
    twitch.watching_channel.set(channel)
    inventory = twitch.inventory.copy()
    wanted = twitch.wanted_games.copy()
    saved = twitch.settings.games_to_watch.copy()
    temporary = twitch.game_order.info()
    progress = [(campaign.progress_revision, campaign.remaining_minutes) for campaign in inventory]
    service = InventoryService(twitch)

    assert service.get_active_campaign(channel) is short
    assert service.get_active_campaign() is short
    assert twitch.inventory == inventory == [long, short]
    assert twitch.wanted_games == wanted
    assert twitch.settings.games_to_watch == saved
    assert twitch.game_order.info() == temporary
    assert [(campaign.progress_revision, campaign.remaining_minutes)
            for campaign in inventory] == progress
    assert twitch.watching_channel.get_with_default(None) is channel


def test_runtime_fallback_and_game_identity_are_not_saved_name_filters(mode):
    twitch = client(mode, 'Saved', priority_list_only=False)
    campaign = add_campaign(twitch, 'fallback', 'Fallback', 2)
    queue(twitch)
    assert twitch.wanted_games == [campaign.game]
    twitch.wanted_games = [Game({'id': '2', 'name': 'Localized name'})]
    channel = _channel(twitch, 100, {'id': '2', 'name': 'Fallback'})

    assert InventoryService(twitch).get_active_campaign(channel) is campaign
    assert twitch.settings.games_to_watch == ['Saved']
