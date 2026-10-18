"""Fresh dashboard snapshots must reflect the current watched channel."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.channel import Channel
from src.web.managers.channels import ChannelListManager


def channels():
    return [Channel.from_directory(MagicMock(), {
        "id": str(1000 + channel_id), "game": {"id": "1", "name": "Fixture game"},
        "viewersCount": 42, "title": "Fixture stream",
        "broadcaster": {"id": str(channel_id), "login": f"fixture{channel_id}",
                        "displayName": f"Fixture {channel_id}"},
    }, drops_enabled=True) for channel_id in (7, 8)]


def populate(manager, targets, batch):
    if batch:
        manager.batch_update(targets)
    else:
        for target in targets:
            manager.display(target, add=True)


def watched(rows):
    return [row["id"] for row in rows if row["watching"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action", ["start", "switch", "clear"])
async def test_snapshot_tracks_watching_transitions_without_redisplaying_channels(batch, action):
    broadcaster = SimpleNamespace(emit=AsyncMock())
    manager = ChannelListManager(broadcaster)
    targets = channels()
    if action != "start":
        manager.set_watching(targets[0])
    populate(manager, targets, batch)
    await asyncio.sleep(0)
    broadcaster.emit.reset_mock()

    if action == "clear":
        manager.clear_watching()
    else:
        manager.set_watching(targets[0 if action == "start" else 1])
    await asyncio.sleep(0)

    assert watched(manager.get_channels()) == ([] if action == "clear" else [7 if action == "start" else 8])
    assert [row["id"] for row in manager.get_channels()] == [7, 8]
    broadcaster.emit.assert_awaited_once_with(
        "channel_watching_clear" if action == "clear" else "channel_watching",
        {} if action == "clear" else {"id": 7 if action == "start" else 8},
    )


@pytest.mark.asyncio
async def test_future_or_removed_watched_id_does_not_mark_an_unrelated_channel():
    manager = ChannelListManager(SimpleNamespace(emit=AsyncMock()))
    first, second = channels()
    manager.set_watching(first)
    manager.display(second, add=True)
    assert watched(manager.get_channels()) == []
    manager.display(first, add=True)
    assert watched(manager.get_channels()) == [7]
    manager.remove(first)
    assert watched(manager.get_channels()) == []
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_snapshots_do_not_rewrite_previously_returned_rows_or_cached_payloads():
    manager = ChannelListManager(SimpleNamespace(emit=AsyncMock()))
    targets = channels()
    manager.batch_update(targets)
    before = manager.get_channels()
    manager.set_watching(targets[0])
    active = manager.get_channels()
    manager.clear_watching()

    assert watched(before) == []
    assert watched(active) == [7]
    assert watched(manager.get_channels()) == []
    assert all(not row["watching"] for row in manager._channels.values())
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "switch", "clear"])
async def test_api_and_reconnect_payloads_share_the_current_watched_channel(monkeypatch, action):
    app = importlib.import_module("src.web.app")
    manager = ChannelListManager(SimpleNamespace(emit=AsyncMock()))
    targets = channels()
    if action != "start":
        manager.set_watching(targets[0])
    manager.batch_update(targets)
    if action == "clear":
        manager.clear_watching()
    else:
        manager.set_watching(targets[0 if action == "start" else 1])
    await asyncio.sleep(0)
    gui = SimpleNamespace(
        channels=manager, status=SimpleNamespace(get=lambda: "Fixture"),
        inv=SimpleNamespace(get_campaigns=lambda: []),
        output=SimpleNamespace(get_history=lambda: []),
        settings=SimpleNamespace(get_settings=lambda: {}),
        login=SimpleNamespace(get_status=lambda: {}),
        progress=SimpleNamespace(get_current_drop=lambda: None), get_wanted_game_tree=lambda: [],
    )
    socket = SimpleNamespace(register=MagicMock(return_value=True), emit=AsyncMock())
    monkeypatch.setattr(app, "gui_manager", gui)
    monkeypatch.setattr(app, "twitch_client", SimpleNamespace(get_manual_mode_info=lambda: {}))
    monkeypatch.setattr(app, "sio", socket)

    api = await app.get_channels()
    await app.connect("fixture", {"asgi.scope": {}})
    event, snapshot = socket.emit.await_args.args

    assert event == "initial_state"
    assert socket.emit.await_args.kwargs == {"room": "fixture"}
    assert api["channels"] == snapshot["channels"]
    assert watched(snapshot["channels"]) == ([] if action == "clear" else [7 if action == "start" else 8])
