"""Atomic settings publication must precede policy wakeups and success broadcasts."""

import asyncio
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.config import State
from src.config.settings import Settings
from src.core.client import Twitch
from src.utils import json_utils
from src.web.managers.settings import SettingsManager
from tests.test_settings_persistence import saved_settings as saved_settings


app = importlib.import_module("src.web.app")
CHANGES = {
    "priority_list_only": False,
    "allow_unlinked_campaigns": True,
    "mining_hours": {"mode": "range", "start": "23:00", "end": "02:00"},
    "discord_webhook_url": "https://discord.com/api/webhooks/123/synthetic-webhook",
}


def policy_owner(state=State.IDLE):
    return SimpleNamespace(
        _state=state, _games_update_pending=False,
        _schedule_changed=asyncio.Event(), _state_change=asyncio.Event(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dump", "fsync", "replace"])
async def test_failed_atomic_api_save_retains_file_policy_and_pending_work(
    saved_settings, monkeypatch, stage,
):
    settings, path = saved_settings
    settings.telegram_bot_token = "synthetic-private-token"
    settings.save()
    before = path.read_bytes()
    previous = {key: deepcopy(getattr(settings, key)) for key in CHANGES}
    owner = policy_owner()
    callback = MagicMock(side_effect=lambda: Twitch.request_games_update(owner))
    broadcaster = SimpleNamespace(emit=AsyncMock())
    manager = SettingsManager(broadcaster, settings, MagicMock(), on_change=callback)
    monkeypatch.setattr(app, "gui_manager", SimpleNamespace(settings=manager))
    failure = OSError("synthetic-private-filesystem-error")

    def partial_dump(contents, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        raise failure

    with monkeypatch.context() as fault:
        if stage == "dump":
            fault.setattr(json_utils.json, "dump", partial_dump)
        else:
            fault.setattr(json_utils.os, stage, MagicMock(side_effect=failure))
        with pytest.raises(HTTPException) as raised:
            await app.update_settings(app.SettingsUpdate(**CHANGES))

    await asyncio.sleep(0)
    assert raised.value.status_code == 500
    assert raised.value.detail == "settings_save_failed"
    assert path.read_bytes() == before
    assert set(path.parent.iterdir()) == {path}
    reloaded = Settings()
    for key, old in previous.items():
        assert getattr(settings, key) == getattr(reloaded, key) == old
    assert not owner._games_update_pending
    assert not owner._schedule_changed.is_set() and not owner._state_change.is_set()
    callback.assert_not_called()
    broadcaster.emit.assert_not_awaited()

    # An explicit retry publishes one complete file before waking the real queue hook.
    response = await app.update_settings(app.SettingsUpdate(**CHANGES))
    await asyncio.sleep(0)
    assert response["success"] is True
    for key, value in CHANGES.items():
        assert getattr(Settings(), key) == value
    assert owner._games_update_pending
    callback.assert_called_once_with()
    broadcaster.emit.assert_awaited_once_with("settings_updated", response["settings"])
    assert "synthetic-private-token" not in json.dumps(response)
    assert CHANGES["discord_webhook_url"] not in json.dumps(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [State.IDLE, State.EXIT])
async def test_complete_file_precedes_settings_delivery_and_queue_admission(
    saved_settings, monkeypatch, state,
):
    settings, path = saved_settings
    owner = policy_owner(state)
    observations = []

    def observe():
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert all(saved[key] == value for key, value in CHANGES.items())
        assert set(path.parent.iterdir()) == {path}
        observations.append(True)

    def change():
        observe()
        return Twitch.request_games_update(owner)

    async def emit(event, payload):
        observe()
        assert event == "settings_updated"
        assert payload["priority_list_only"] is False
        assert payload["discord_configured"] is True
        assert payload["discord_webhook_url"] != CHANGES["discord_webhook_url"]

    broadcaster = SimpleNamespace(emit=AsyncMock(side_effect=emit))
    manager = SettingsManager(broadcaster, settings, MagicMock(), on_change=change)
    monkeypatch.setattr(app, "gui_manager", SimpleNamespace(settings=manager))
    response = await app.update_settings(app.SettingsUpdate(**CHANGES))
    await asyncio.sleep(0)
    assert response["success"] is True and len(observations) == 2
    assert owner._games_update_pending is (state is State.IDLE)
    assert owner._state_change.is_set() is (state is State.IDLE)
