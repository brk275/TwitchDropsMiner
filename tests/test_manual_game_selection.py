"""Manual game intent survives live callbacks and core channel reselection."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import State
from src.models.channel import Stream
from tests.test_deadline_service import scenario
from tests.test_priority_fallback import _campaign, _channel
from tests.test_session_lifecycle import miner


def setup_client(tmp_path, monkeypatch):
    _, owner, _, high, low, _ = scenario()
    client = miner(tmp_path, monkeypatch)
    client.settings = owner.settings
    client.inventory = [high, low]
    client.wanted_games = [high.game, low.game]
    for campaign in client.inventory:
        campaign._twitch = client
        for drop in campaign.drops:
            drop._twitch = client
    client._drops = {d.id: d for c in client.inventory for d in c.drops}
    high_channel = _channel(client, 100, {"id": "1", "name": "High"})
    manual_channel = _channel(client, 200, {"id": "2", "name": "Low"})
    alternate = _channel(client, 201, {"id": "2", "name": "Low"})
    client.channels.update({c.id: c for c in (high_channel, manual_channel, alternate)})
    client.gui.channels.get_selection.return_value = None
    client.gql_request = AsyncMock(side_effect=AssertionError("No live requests"))
    client.enter_manual_mode(manual_channel)
    client.watch(manual_channel, update_status=False)
    return client, high_channel, manual_channel, alternate


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["online", "update"])
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("current_state", ["healthy", "offline"])
async def test_other_category_callback_respects_manual_game(tmp_path, monkeypatch, event, manual, current_state):
    client, high, selected, _ = setup_client(tmp_path, monkeypatch)
    if not manual:
        client.clear_manual_mode("Automatic control")
    if current_state == "offline":
        selected._stream = None
    before = None if event == "online" else high._stream
    client._message_handler_service.on_channel_update(high, before, high._stream)
    assert client.watching_channel.get_with_default(None) is (selected if manual else high)
    assert client.drop_history.total_count == 0
    client.gql_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_watched_manual_category_change_requests_reselection(tmp_path, monkeypatch):
    client, _, selected, _ = setup_client(tmp_path, monkeypatch)
    before = selected._stream
    selected._stream = Stream(selected, id=200, game={"id": "1", "name": "High"}, viewers=10, title="Live")
    client._message_handler_service.on_channel_update(selected, before, selected._stream)
    assert client._state is State.CHANNEL_SWITCH
    assert client._manual_target_game.name == "Low"
    assert client.drop_history.total_count == 0
    client.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["online", "update"])
async def test_manual_game_first_ranking_control(tmp_path, monkeypatch, event):
    client, high, selected, _ = setup_client(tmp_path, monkeypatch)
    client.wanted_games = [selected.game, high.game]
    before = None if event == "online" else high._stream
    client._message_handler_service.on_channel_update(high, before, high._stream)
    assert client.watching_channel.get_with_default(None) is selected
    assert client.settings.games_to_watch == ["High", "Low"]
    client.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["online", "update"])
async def test_same_game_recovery_control(tmp_path, monkeypatch, event):
    client, _, selected, alternate = setup_client(tmp_path, monkeypatch)
    intended_game = client._manual_target_game
    selected._stream = None
    before = None if event == "online" else alternate._stream
    client._message_handler_service.on_channel_update(alternate, before, alternate._stream)
    assert client.watching_channel.get_with_default(None) is alternate
    assert client.is_manual_mode() and client._manual_target_game is intended_game
    assert client.drop_history.total_count == 0
    client.gql_request.assert_not_awaited()


@pytest.mark.parametrize("current", ["healthy", "offline", "wrong-game", "none"])
@pytest.mark.parametrize("acl", [False, True])
def test_manual_switch_policy_keeps_same_game_ties_and_acl_order(tmp_path, monkeypatch, current, acl):
    client, high, selected, alternate = setup_client(tmp_path, monkeypatch)
    selected.acl_based = False
    alternate.acl_based = acl
    if current == "offline":
        selected._stream = None
    elif current == "wrong-game":
        selected._stream.game = high.game
    elif current == "none":
        client.watching_channel.clear()
    assert client.can_watch(high)
    assert not client.should_switch(high)
    assert client.should_switch(alternate) is (current != "healthy" or acl)
    assert client.is_manual_mode() and client._manual_target_game.name == "Low"
    client.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_alternate", [False, True])
@pytest.mark.parametrize("change", ["category", "offline", "drops-disabled", "banned"])
@pytest.mark.parametrize("route", ["forced", "callback"])
@pytest.mark.parametrize("shutdown", ["scheduled", "synchronous"])
async def test_real_switch_rejects_changed_manual_channel(
    tmp_path, monkeypatch, has_alternate, change, route, shutdown,
):
    client, high, selected, alternate = setup_client(tmp_path, monkeypatch)
    # Start a fresh run, then enter manual mode after its initial game ordering.
    client.clear_manual_mode("Fresh state machine")
    client.stop_watching()
    client.channels.clear()
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=type("Identity", (), {"user_id": 42})())
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(side_effect=lambda channels, **kw: list(channels))
    client.get_live_streams = AsyncMock(side_effect=lambda game, **kw: [high] if game.id == 1
                                      else [selected, alternate] if has_alternate else [selected])
    client.gui.channels.batch_update.side_effect = lambda channels: client.enter_manual_mode(selected)
    watched = []
    original = client.watch
    exit_states = []

    def mode_changed(info):
        if not info["active"]:
            def finish():
                exit_states.append(client._state)
                client.close()
            if shutdown == "synchronous":
                finish()
            else:
                asyncio.get_running_loop().call_soon(finish)

    client.gui.broadcast_manual_mode_change.side_effect = mode_changed

    def watch(channel, **kwargs):
        original(channel, **kwargs)
        if kwargs.get("update_status", True):
            watched.append(channel)
            if len(watched) == 1:
                def category_change():
                    before = selected._stream
                    if change == "category":
                        selected._stream = Stream(selected, id=200, game={"id": "1", "name": "High"},
                                                  viewers=10, title="New category")
                    elif change == "offline":
                        selected._stream = None
                    elif change == "drops-disabled":
                        selected._stream.drops_enabled = False
                    else:
                        selected.update_ban_status({"data": {"user": {
                            "id": str(selected.id), "self": {"banStatus": {"isPermanent": True}},
                        }}})
                    if route == "callback":
                        client.on_channel_update(selected, before, selected._stream)
                    else:
                        client.change_state(State.CHANNEL_SWITCH)
                asyncio.get_running_loop().call_soon(category_change)
            else:
                asyncio.get_running_loop().call_soon(client.close)

    client.watch = MagicMock(side_effect=watch)
    try:
        await asyncio.wait_for(client._run(), 3)
        assert watched[0] is selected
        assert client.is_manual_mode() is has_alternate
        if has_alternate:
            assert len(watched) == 2
            assert watched[1] is alternate
            assert client._manual_target_game.name == "Low"
        elif shutdown == "scheduled":
            current = client.watching_channel.get_with_default(None)
            assert current is not None and current.game == high.game
            assert client.can_watch(current)
            assert not client.should_switch(high)
            assert exit_states == [State.CHANNEL_SWITCH]
        else:
            assert len(watched) == 1
            assert exit_states == [State.CHANNEL_SWITCH]
        assert client._state is State.EXIT
        assert client.settings.games_to_watch == ["High", "Low"]
        assert client.drop_history.total_count == 0
        client.gql_request.assert_not_awaited()
    finally:
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)


@pytest.mark.parametrize("transition", ["clear", "exit"])
def test_manual_exit_restores_saved_priorities_before_broadcast(tmp_path, monkeypatch, transition):
    client, high, selected, _ = setup_client(tmp_path, monkeypatch)
    client.wanted_games = [selected.game, high.game]
    client._state = State.IDLE
    client._state_change.clear()
    observed = []

    def mode_changed(info):
        observed.append((info, list(client.wanted_games)))
        client.close()

    client.gui.broadcast_manual_mode_change.side_effect = mode_changed
    if transition == "clear":
        assert client.clear_manual_mode("Return to automatic")
    else:
        client.exit_manual_mode("Return to automatic")
    assert observed == [({"active": False}, [high.game, selected.game])]
    assert client._state is State.EXIT and client._state_change.is_set()
    assert not client.clear_manual_mode("Already automatic")
    assert len(observed) == 1
    assert client.settings.games_to_watch == ["High", "Low"]
    assert client.drop_history.total_count == 0
    client.gql_request.assert_not_awaited()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("case", [
    "same-game", "other-game", "offline", "banned", "drops-disabled", "ignored",
    "unlinked", "expired", "claimed", "disabled-benefit", "missing-prerequisite", "hours",
])
def test_mode_policy_preserves_earning_gates_and_observations(tmp_path, monkeypatch, manual, case):
    client, high, selected, _ = setup_client(tmp_path, monkeypatch)
    if not manual:
        client.clear_manual_mode("Automatic control")
    campaign = client.inventory[1]
    drop = campaign.timed_drops["low-drop"]
    candidate = selected
    if case == "other-game":
        candidate = high
    elif case == "offline":
        selected._stream = None
    elif case == "banned":
        selected.update_ban_status({"data": {"user": {
            "id": str(selected.id), "self": {"banStatus": {"isPermanent": True}},
        }}})
    elif case == "drops-disabled":
        selected._stream.drops_enabled = False
    elif case == "ignored":
        client.settings.drop_name_blacklist = ["low-drop"]
    elif case == "unlinked":
        campaign.linked = False
    elif case == "expired":
        campaign.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif case == "claimed":
        drop.is_claimed = True
    elif case == "disabled-benefit":
        client.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
    elif case == "missing-prerequisite":
        drop.precondition_drops = ["missing"]
    elif case == "hours":
        client.is_mining_time = lambda: False
    observed = [(item.real_current_minutes, item.extra_current_minutes, item.is_claimed, item.claim_id)
                for item in client._drops.values()]
    expected_raw = case in ("same-game", "other-game")
    assert client.can_watch(candidate) is expected_raw
    assert client.can_watch_in_current_mode(candidate) is (
        expected_raw and (not manual or case != "other-game")
    )
    assert observed == [(item.real_current_minutes, item.extra_current_minutes, item.is_claimed, item.claim_id)
                        for item in client._drops.values()]
    assert client.settings.games_to_watch == ["High", "Low"]
    assert client.drop_history.total_count == 0
    client.gql_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,event_game,strict,promoted", [
    ("other-game", None, True, True),
    ("same-game", None, True, True),
    ("special-other", (509663, "Special Events"), True, True),
    *[("special-uncategorized", game, strict, promoted)
      for game in ((509663, "Special Events"), (509672, "IRL"))
      for strict in (False, True) for promoted in (False, True)],
])
async def test_explicit_selection_replaces_manual_intent_without_narrowing_eligibility(
    tmp_path, monkeypatch, choice, event_game, strict, promoted,
):
    client, high, selected, alternate = setup_client(tmp_path, monkeypatch)
    client.settings.priority_list_only = strict
    candidate = alternate if choice == "same-game" else high
    automatic_high = _channel(client, 101, {"id": "1", "name": "High"})
    if choice.startswith("special-"):
        if choice == "special-other":
            client.inventory.pop(0)
        special = _campaign("event", event_game[1], game_id=event_game[0],
                            settings=client.settings, acl=(candidate.id,))
        special._twitch = client
        for drop in special.drops:
            drop._twitch = client
        special.allowed_channels = [candidate]
        client.inventory.append(special)
        client.settings.games_to_watch.append(event_game[1])
        candidate._stream.drops_enabled = False
        if choice == "special-uncategorized":
            candidate._stream.game = None
    client._drops = {drop.id: drop for campaign in client.inventory for drop in campaign.drops}
    if not promoted:
        client.clear_manual_mode("Enter manual mode after initial ordering")
    saved = list(client.settings.games_to_watch)
    client._ensure_api_clients = MagicMock()
    client.get_auth = AsyncMock(return_value=type("Identity", (), {"user_id": 42})())
    client.fetch_inventory = AsyncMock()
    client.websocket.start = AsyncMock()
    client._watch_service.watch_loop = AsyncMock()
    client._mining_schedule_loop = AsyncMock()
    client.bulk_check_online = AsyncMock()
    client._channel_service.filter_banned_channels = AsyncMock(side_effect=lambda channels, **kw: list(channels))
    client.get_live_streams = AsyncMock(side_effect=lambda game, **kw: [
        channel for channel in (high, selected, alternate, automatic_high) if channel.game == game
    ])

    def choose(channels):
        if not client.is_manual_mode():
            client.enter_manual_mode(selected)
        assert candidate.id in client.channels
        assert client.can_watch(candidate)
        assert client.can_watch_in_current_mode(candidate) is (choice == "same-game")
        client.gui.channels.get_selection.return_value = candidate

    client.gui.channels.batch_update.side_effect = choose
    original = client.watch
    after_callbacks = []

    def observe_automatic_handoffs():
        client.on_channel_update(automatic_high, None, automatic_high._stream)
        after_callbacks.append(client.watching_channel.get_with_default(None))
        client.on_channel_update(selected, None, selected._stream)
        after_callbacks.append(client.watching_channel.get_with_default(None))
        client.close()

    def watch(channel, **kwargs):
        original(channel, **kwargs)
        if kwargs.get("update_status", True):
            if choice != "special-uncategorized":
                asyncio.get_running_loop().call_soon(client.close)
            elif channel is candidate:
                assert not client.is_manual_mode()
                asyncio.get_running_loop().call_soon(observe_automatic_handoffs)

    client.watch = MagicMock(side_effect=watch)
    try:
        await asyncio.wait_for(client._run(), 3)
        if choice == "special-uncategorized":
            assert after_callbacks == [automatic_high, automatic_high]
            assert not client.is_manual_mode()
            assert client._manual_target_channel is None and client._manual_target_game is None
            assert client.wanted_games == client._stream_selector.get_wanted_games(client.settings, client.inventory)
        else:
            assert client.watching_channel.get_with_default(None) is candidate
            assert client.is_manual_mode()
            assert client._manual_target_channel is candidate
            assert client._manual_target_game == candidate.game
            assert client.get_manual_mode_info()["channel_name"] == candidate.name
        assert client.settings.games_to_watch == saved
        assert client.drop_history.total_count == 0
        client.gql_request.assert_not_awaited()
    finally:
        await client._stop_mining_schedule()
        if client._watching_task is not None:
            client._watching_task.cancel()
            await asyncio.gather(client._watching_task, return_exceptions=True)
