"""Manual game intent survives callbacks without overriding earning or queue policy."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.core.client import Twitch
from src.models.channel import Stream
from src.services.channel_service import ChannelService
from src.services.game_order import TemporaryGameOrder
from src.services.message_handlers import MessageHandlerService
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService
from src.utils import AwaitableValue
from tests.test_priority_fallback import _campaign, _channel, _drop, _settings


def manual_client():
    # No constructor: authentication, browser profiles, storage and transports stay absent.
    client = object.__new__(Twitch)
    client.settings = _settings("Alpha", "Beta", "Gamma", priority_list_only=True)
    client.gui = MagicMock()
    client.gui.channels.get_selection.return_value = None
    client.print = MagicMock()
    client.is_mining_time = lambda: True
    client._manual_target_channel = client._manual_target_game = None
    client.game_order = TemporaryGameOrder()
    client.game_order.set(["Beta", "Alpha"])
    client._stream_selector = StreamSelector()
    client.inventory = [
        _campaign(name.lower(), name, game_id=index, settings=client.settings,
                  drops=[_drop(f"{name}-reward")])
        for index, name in enumerate(("Alpha", "Beta", "Gamma"), 1)
    ]
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client._drops = {drop.id: drop for campaign in client.inventory for drop in campaign.drops}
    alpha, beta, gamma = [
        _channel(client, index * 100, {"id": str(index), "name": name})
        for index, name in enumerate(("Alpha", "Beta", "Gamma"), 1)
    ]
    alternate = _channel(client, 101, {"id": "1", "name": "Alpha"})
    client.channels = OrderedDict((channel.id, channel) for channel in (alpha, alternate, beta, gamma))
    client.watching_channel = AwaitableValue()
    client._channel_service = ChannelService(client)
    client._watch_service = WatchService(client)
    client._message_handler_service = MessageHandlerService(client)
    client.gql_request = AsyncMock(side_effect=AssertionError("No live requests"))
    client.wanted_games = [campaign.game for campaign in client.inventory]
    client._state = State.CHANNEL_SWITCH
    client._state_change = asyncio.Event()
    client._resume_mining = asyncio.Event()
    client._schedule_changed = asyncio.Event()
    client._browser = None
    client._schedule_task = client._watching_task = None
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    client.websocket = MagicMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.request_inventory_refresh = MagicMock()
    client._activate_pending_inventory_refresh = MagicMock()
    client._activate_pending_games_update = MagicMock()
    client.get_active_campaign = MagicMock(return_value=None)
    client.enter_manual_mode(alpha)
    client.watch(alpha, update_status=False)
    return client, alpha, alternate, beta, gamma


async def run_selection(client):
    task = asyncio.create_task(client._run())
    try:
        # Let the real selection branch and any queued synthetic callbacks settle.
        for _ in range(8):
            await asyncio.sleep(0)
        client.close()
        await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)
    client.gql_request.assert_not_awaited()


@pytest.mark.parametrize("event", ["online", "metadata"])
@pytest.mark.parametrize("manual", [True, False])
@pytest.mark.parametrize("current", ["healthy", "offline"])
def test_temporal_callback_respects_manual_game_and_automatic_control(event, manual, current):
    client, alpha, _, beta, _ = manual_client()
    if not manual:
        client.clear_manual_mode("Automatic control")
    if current == "offline":
        alpha._stream = None
    before = None if event == "online" else beta._stream
    client.on_channel_update(beta, before, beta._stream)
    assert client.watching_channel.get_with_default(None) is (alpha if manual else beta)
    assert client.is_manual_mode() is manual
    assert client.settings.games_to_watch == ["Alpha", "Beta", "Gamma"]
    client.gql_request.assert_not_awaited()


@pytest.mark.parametrize("event", ["online", "metadata"])
def test_watched_manual_category_change_requests_reselection(event):
    client, alpha, _, beta, _ = manual_client()
    client._state = State.IDLE
    before = None if event == "online" else alpha._stream
    alpha._stream = Stream(alpha, id=100, game={"id": "2", "name": "Beta"},
                           viewers=10, title="Changed category")
    assert client.can_watch(alpha) and alpha.game == beta.game
    client.on_channel_update(alpha, before, alpha._stream)
    assert client._state is State.CHANNEL_SWITCH
    assert client._manual_target_game.name == "Alpha"


@pytest.mark.parametrize("event", ["online", "metadata"])
def test_same_game_callback_recovers_without_losing_manual_intent(event):
    client, alpha, alternate, beta, _ = manual_client()
    intended = client._manual_target_game
    alpha._stream = None
    before = None if event == "online" else alternate._stream
    client.on_channel_update(alternate, before, alternate._stream)
    assert client.watching_channel.get_with_default(None) is alternate
    assert client._manual_target_game is intended
    assert not client.should_switch(beta)


@pytest.mark.parametrize("current", ["healthy", "offline", "wrong-game", "none"])
@pytest.mark.parametrize("acl", [False, True])
def test_manual_same_game_switch_keeps_ties_acl_preference_and_recovery(current, acl):
    client, alpha, alternate, beta, _ = manual_client()
    alpha.acl_based = False
    alternate.acl_based = acl
    if current == "offline":
        alpha._stream = None
    elif current == "wrong-game":
        alpha._stream.game = beta.game
    elif current == "none":
        client.watching_channel.clear()
    assert not client.should_switch(beta)
    assert client.should_switch(alternate) is (current != "healthy" or acl)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_alternate", [False, True])
@pytest.mark.parametrize("change", ["category", "offline", "drops-disabled", "banned"])
async def test_state_selection_revalidates_manual_target_and_falls_through(change, has_alternate):
    client, alpha, alternate, beta, gamma = manual_client()
    client.game_order.set(["Gamma", "Beta", "Alpha"])
    before = alpha._stream
    if change == "category":
        alpha._stream = Stream(alpha, id=100, game={"id": "2", "name": "Beta"},
                               viewers=10, title="Changed category")
        assert client.can_watch(alpha)
    elif change == "offline":
        alpha._stream = None
    elif change == "drops-disabled":
        alpha._stream.drops_enabled = False
    else:
        alpha.update_ban_status({"data": {"user": {
            "id": "100", "self": {"banStatus": {"isPermanent": True}},
        }}})
    if not has_alternate:
        del client.channels[alternate.id]
    client.on_channel_update(alpha, before, alpha._stream)
    await run_selection(client)
    assert client.watching_channel.get_with_default(None) is (alternate if has_alternate else gamma)
    assert client.is_manual_mode() is has_alternate
    if has_alternate:
        assert client._manual_target_channel is alternate
        assert client._manual_target_game.name == "Alpha"
    else:
        assert client.wanted_games == [gamma.game, beta.game, client.inventory[0].game]
    assert client.settings.games_to_watch == ["Alpha", "Beta", "Gamma"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["same-game", "other-game", "changed-current-game", "uncategorized"])
async def test_explicit_choice_can_replace_manual_target(choice):
    client, alpha, alternate, beta, _ = manual_client()
    candidate = alternate if choice == "same-game" else beta
    if choice == "changed-current-game":
        alpha._stream.game = beta.game
    if choice == "uncategorized":
        special = _campaign("event", "Special Events", game_id=509663,
                            settings=client.settings, acl=(beta.id,))
        client.inventory.append(special)
        client.settings.games_to_watch.append("Special Events")
        client.wanted_games.append(special.game)
        beta._stream.game = None
        beta._stream.drops_enabled = False
    assert client.can_watch(candidate)
    client.gui.channels.get_selection.return_value = candidate
    await run_selection(client)
    assert client.watching_channel.get_with_default(None) is candidate
    if choice == "uncategorized":
        assert not client.is_manual_mode()
        assert client.wanted_games == client._stream_selector.get_wanted_games(
            client.settings, client.inventory, client.game_order,
        )
    else:
        assert client._manual_target_channel is candidate
        assert client._manual_target_game == candidate.game


@pytest.mark.parametrize("transition", ["clear", "exit"])
@pytest.mark.parametrize("order", ["active", "cleared", "inactive"])
def test_manual_exit_restores_current_queue_order_before_reentrant_publication(transition, order):
    client, alpha, _, beta, gamma = manual_client()
    client.enter_manual_mode(beta)
    client.wanted_games = [beta.game, alpha.game, gamma.game]
    if order == "active":
        client.game_order.set(["Gamma", "Beta", "Alpha"])
    elif order == "cleared":
        client.game_order.clear()
    else:
        client.game_order = TemporaryGameOrder()
    expected = [gamma.game, beta.game, alpha.game] if order == "active" else [alpha.game, beta.game, gamma.game]
    observed = []

    def publish(info):
        observed.append((info, list(client.wanted_games)))
        client.close()

    client.gui.broadcast_manual_mode_change.side_effect = publish
    if transition == "clear":
        assert client.clear_manual_mode("Return to automatic")
    else:
        client.exit_manual_mode("Return to automatic")
    assert observed == [({"active": False}, expected)]
    assert client._state is State.EXIT and client._state_change.is_set()
    assert not client.clear_manual_mode("Already automatic")
    assert len(observed) == 1
    assert client.settings.games_to_watch == ["Alpha", "Beta", "Gamma"]


@pytest.mark.asyncio
@pytest.mark.parametrize("publication", ["enter", "clear"])
async def test_selection_does_not_watch_or_consume_exit_from_mode_publication(publication):
    client, alpha, alternate, beta, _ = manual_client()
    if publication == "enter":
        client.gui.channels.get_selection.return_value = beta
    else:
        alpha._stream = None
        del client.channels[alternate.id]
    client.gui.broadcast_manual_mode_change.side_effect = lambda info: client.close()
    client.watch = MagicMock(wraps=client.watch)
    await run_selection(client)
    client.watch.assert_not_called()
    assert client._state is State.EXIT and client._state_change.is_set()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("case", [
    "same-game", "other-game", "offline", "banned", "drops-disabled", "ignored",
    "unlinked", "expired", "claimed", "disabled-benefit", "missing-prerequisite", "hours", "pause",
])
def test_mode_policy_preserves_earning_gates_and_progress(manual, case):
    client, alpha, _, beta, _ = manual_client()
    if not manual:
        client.clear_manual_mode("Automatic control")
    campaign = client.inventory[0]
    drop = next(iter(campaign.drops))
    candidate = beta if case == "other-game" else alpha
    if case == "offline":
        alpha._stream = None
    elif case == "banned":
        alpha.update_ban_status({"data": {"user": {
            "id": "100", "self": {"banStatus": {"isPermanent": True}},
        }}})
    elif case == "drops-disabled":
        alpha._stream.drops_enabled = False
    elif case == "ignored":
        client.settings.drop_name_blacklist = [drop.name]
    elif case == "unlinked":
        campaign.linked = False
    elif case == "expired":
        campaign.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == "claimed":
        drop.is_claimed = True
    elif case == "disabled-benefit":
        client.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
        client.wanted_games = client._stream_selector.get_wanted_games(
            client.settings, client.inventory, client.game_order,
        )
    elif case == "missing-prerequisite":
        drop.precondition_drops = ["missing"]
    elif case == "hours":
        client.is_mining_time = lambda: False
    elif case == "pause":
        client._paused = True
        client._pause_deadline = None
        client._resume_at = None
        client.is_mining_time = lambda: Twitch.is_mining_time(client)
    before = (drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed, drop.claim_id)
    raw = case in ("same-game", "other-game")
    assert client.can_watch(candidate) is raw
    assert client.can_watch_in_current_mode(candidate) is (raw and (not manual or case != "other-game"))
    assert (drop.real_current_minutes, drop.extra_current_minutes, drop.is_claimed, drop.claim_id) == before
    client.gql_request.assert_not_awaited()
