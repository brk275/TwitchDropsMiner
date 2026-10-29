"""Synthetic fixtures shared by channel refresh ownership regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.models.channel import Channel, Stream
from tests.test_priority_fallback import _campaign, _channel, _settings
from tests.test_session_lifecycle import miner


GAME = {"id": "1", "name": "Game"}


def force_available_drops_branch(monkeypatch):
    original = Stream.from_get_stream.__func__

    def disabled(cls, channel, data):
        result = original(cls, channel, data)
        result.drops_enabled = False
        return result

    monkeypatch.setattr(Stream, "from_get_stream", classmethod(disabled))


def client():
    return SimpleNamespace(
        gui=MagicMock(), _campaigns={},
        settings=SimpleNamespace(drop_name_blacklist=[], mining_benefits={"DIRECT_ENTITLEMENT": True}),
    )


def channel(twitch, channel_id=100, *, online=True):
    result = Channel.from_directory(twitch, {
        "id": "123456", "game": GAME, "viewersCount": 42, "title": "Old stream",
        "broadcaster": {"id": str(channel_id), "login": f"fixture{channel_id}", "displayName": "Fixture"},
    }, drops_enabled=True)
    if not online:
        result._stream = None
    return result


def live(channel_id):
    return {"data": {"user": {
        "id": str(channel_id), "stream": {"id": "999999", "viewersCount": 60},
        "broadcastSettings": {"game": GAME, "title": "New stream"},
    }}}


def setup_client(tmp_path, monkeypatch):
    owner = miner(tmp_path, monkeypatch)
    owner.settings = _settings("High", "Low", priority_list_only=True)
    owner.inventory = [_campaign("high", "High", game_id=1, settings=owner.settings),
                       _campaign("low", "Low", game_id=2, settings=owner.settings)]
    owner.wanted_games = [campaign.game for campaign in owner.inventory]
    for campaign in owner.inventory:
        campaign._twitch = owner
        for drop in campaign.drops:
            drop._twitch = owner
    owner._drops = {drop.id: drop for campaign in owner.inventory for drop in campaign.drops}
    owner._campaigns = {campaign.id: campaign for campaign in owner.inventory}
    high = _channel(owner, 100, {"id": "1", "name": "High"})
    selected = _channel(owner, 200, {"id": "2", "name": "Low"})
    alternate = _channel(owner, 201, {"id": "2", "name": "Low"})
    owner.channels.update({item.id: item for item in (high, selected, alternate)})
    owner.gui.channels.get_selection.return_value = None
    owner.gql_request = AsyncMock(side_effect=AssertionError("No live requests"))
    owner.enter_manual_mode(selected)
    owner.watch(selected, update_status=False)
    return owner, high, selected, alternate
