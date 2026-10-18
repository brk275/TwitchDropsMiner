"""HTTP refresh settings use real persistence and recover after failed writes."""

import asyncio
import errno
import importlib
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from src.config.settings import Settings
from src.utils import json_utils
from src.web.managers.settings import SettingsManager


web_app = importlib.import_module("src.web.app")
STORED_TOKEN = "FIXTURE-STORED-TOKEN"
STORAGE_DETAIL = "FIXTURE-STORAGE-DETAIL"


@pytest.fixture
def settings_api(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("src.config.settings.SETTINGS_PATH", path)
    settings = Settings()
    settings.games_to_watch = ["Keep ranked game"]
    settings.telegram_bot_token = STORED_TOKEN
    settings.save()
    broadcaster, console, callback = AsyncMock(), MagicMock(), MagicMock()
    manager = SettingsManager(broadcaster, settings, console, on_change=callback)
    monkeypatch.setattr(web_app, "gui_manager", SimpleNamespace(settings=manager))
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, web_app.validation_error)
    app.get("/api/settings")(web_app.get_settings)
    app.post("/api/settings")(web_app.update_settings)
    return SimpleNamespace(app=app, settings=settings, path=path, broadcaster=broadcaster,
                           console=console, callback=callback)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (1, 1440))
@pytest.mark.parametrize("operation", ("dump", "fsync", "replace"))
async def test_failed_http_save_preserves_disk_live_interval_and_allows_retry(settings_api, monkeypatch,
                                                                          value, operation):
    state = settings_api
    before = state.path.read_bytes()
    failure = OSError(errno.ENOSPC, STORAGE_DETAIL)

    def partial_dump(contents, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        raise failure

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app),
                                 base_url="http://miner.test") as client:
        with monkeypatch.context() as fault:
            if operation == "dump":
                fault.setattr(json_utils.json, "dump", partial_dump)
            else:
                fault.setattr(os, operation, MagicMock(side_effect=failure))
            failed = await client.post("/api/settings", json={"minimum_refresh_interval_minutes": value})
        await asyncio.sleep(0)
        assert failed.status_code == 500
        assert failed.json() == {"detail": "settings_save_failed"}
        assert STORAGE_DETAIL not in failed.text and STORED_TOKEN not in failed.text
        assert state.path.read_bytes() == before
        assert state.settings.minimum_refresh_interval_minutes == 30
        assert Settings().minimum_refresh_interval_minutes == 30
        assert set(state.path.parent.iterdir()) == {state.path}
        state.broadcaster.emit.assert_not_called()
        state.console.print.assert_not_called()
        state.callback.assert_not_called()
        current = await client.get("/api/settings")
        assert current.json()["minimum_refresh_interval_minutes"] == 30
        assert STORED_TOKEN not in current.text

        saved = await client.post("/api/settings", json={"minimum_refresh_interval_minutes": value})
        fetched = await client.get("/api/settings")
    await asyncio.sleep(0)
    assert saved.status_code == fetched.status_code == 200
    assert saved.json()["success"] is True
    assert saved.json()["settings"]["minimum_refresh_interval_minutes"] == value
    assert fetched.json()["minimum_refresh_interval_minutes"] == value
    assert STORED_TOKEN not in saved.text and STORED_TOKEN not in fetched.text
    loaded = Settings()
    assert loaded.minimum_refresh_interval_minutes == value
    assert loaded.games_to_watch == ["Keep ranked game"]
    assert loaded.telegram_bot_token == STORED_TOKEN
    assert set(state.path.parent.iterdir()) == {state.path}
    state.broadcaster.emit.assert_awaited_once_with("settings_updated", saved.json()["settings"])
    state.console.print.assert_called_once()
    state.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (0, -1, 1441, True, 1.5, "30"))
async def test_invalid_http_input_is_rejected_before_staging_a_file(settings_api, monkeypatch, value):
    state = settings_api
    before = state.path.read_bytes()
    staging = MagicMock(wraps=tempfile.NamedTemporaryFile)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", staging)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app),
                                 base_url="http://miner.test") as client:
        response = await client.post("/api/settings", json={
            "minimum_refresh_interval_minutes": value,
            "games_to_watch": ["Must not become live"],
            "telegram_bot_token": "FIXTURE-REJECTED-TOKEN",
        })
    await asyncio.sleep(0)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert "FIXTURE-REJECTED-TOKEN" not in response.text
    assert state.path.read_bytes() == before
    assert state.settings.minimum_refresh_interval_minutes == 30
    assert state.settings.games_to_watch == ["Keep ranked game"]
    assert state.settings.telegram_bot_token == STORED_TOKEN
    staging.assert_not_called()
    state.broadcaster.emit.assert_not_called()
    state.console.print.assert_not_called()
    state.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (1, 30, 1440))
async def test_http_round_trip_retains_ranked_games_and_private_settings(settings_api, value):
    state = settings_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app),
                                 base_url="http://miner.test") as client:
        saved = await client.post("/api/settings", json={"minimum_refresh_interval_minutes": value})
        fetched = await client.get("/api/settings")
    await asyncio.sleep(0)
    assert saved.status_code == fetched.status_code == 200
    assert saved.json()["settings"] == fetched.json()
    assert saved.json()["settings"]["minimum_refresh_interval_minutes"] == value
    assert saved.json()["settings"]["games_to_watch"] == ["Keep ranked game"]
    assert STORED_TOKEN not in saved.text and STORED_TOKEN not in fetched.text
    loaded = Settings()
    assert loaded.minimum_refresh_interval_minutes == value
    assert loaded.games_to_watch == ["Keep ranked game"]
    assert loaded.telegram_bot_token == STORED_TOKEN
    assert set(state.path.parent.iterdir()) == {state.path}
    state.broadcaster.emit.assert_awaited_once_with("settings_updated", saved.json()["settings"])
    state.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits do not establish Windows directory ACLs")
async def test_read_only_parent_returns_save_error_without_in_place_fallback(settings_api):
    if os.geteuid() == 0:
        pytest.skip("Root can bypass directory write permissions")
    state = settings_api
    before = state.path.read_bytes()
    mode = state.path.parent.stat().st_mode
    try:
        state.path.parent.chmod(0o500)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app),
                                     base_url="http://miner.test") as client:
            response = await client.post("/api/settings", json={"minimum_refresh_interval_minutes": 1})
        assert response.status_code == 500
        assert response.json() == {"detail": "settings_save_failed"}
        assert state.path.read_bytes() == before
        assert state.settings.minimum_refresh_interval_minutes == 30
    finally:
        state.path.parent.chmod(mode)
    await asyncio.sleep(0)
    state.broadcaster.emit.assert_not_called()
    state.callback.assert_not_called()
    state.console.print.assert_not_called()
    assert Settings().minimum_refresh_interval_minutes == 30
    assert set(state.path.parent.iterdir()) == {state.path}
