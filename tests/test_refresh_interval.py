"""Refresh intervals stay bounded across settings, HTTP and maintenance."""

import asyncio
import importlib
import json
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from src.config.settings import Settings
from src.services.maintenance import MaintenanceService
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


web_app = importlib.import_module("src.web.app")
INVALID_INTERVALS = (0, -1, 1441, 10**100, True, False, 1.0, 1.5, "30", [], {})


@pytest.fixture
def managed_settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("src.config.settings.SETTINGS_PATH", path)
    settings = Settings()
    settings.save()
    broadcaster, console, callback = AsyncMock(), MagicMock(), MagicMock()
    manager = SettingsManager(broadcaster, settings, console, on_change=callback)
    return SimpleNamespace(settings=settings, manager=manager, path=path,
                           broadcaster=broadcaster, console=console, callback=callback)


@pytest.fixture
def settings_http(managed_settings, monkeypatch):
    monkeypatch.setattr(web_app, "gui_manager", SimpleNamespace(settings=managed_settings.manager))
    application = FastAPI()
    application.add_exception_handler(RequestValidationError, web_app.validation_error)
    application.post("/api/settings")(web_app.update_settings)
    application.get("/api/settings")(web_app.get_settings)
    return application


@pytest.mark.parametrize("value", INVALID_INTERVALS)
def test_api_rejects_invalid_interval(value):
    with pytest.raises(ValidationError):
        SettingsUpdate(minimum_refresh_interval_minutes=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", INVALID_INTERVALS)
async def test_manager_rejects_interval_before_other_mutation(managed_settings, value):
    state = managed_settings
    before = state.path.read_bytes()
    try:
        with pytest.raises(ValueError):
            state.manager.update_settings({
                "minimum_refresh_interval_minutes": value,
                "games_to_watch": ["Must not become live"],
                "dark_mode": True,
            })
    finally:
        await asyncio.sleep(0)
    assert state.settings.minimum_refresh_interval_minutes == 30
    assert state.settings.games_to_watch == []
    assert state.settings.dark_mode is False
    assert state.path.read_bytes() == before
    state.callback.assert_not_called()
    state.console.print.assert_not_called()
    state.broadcaster.emit.assert_not_called()


@pytest.mark.parametrize("value", (*INVALID_INTERVALS, None))
def test_invalid_saved_interval_uses_default(managed_settings, value):
    state = managed_settings
    state.path.write_text(json.dumps({"minimum_refresh_interval_minutes": value,
                                      "games_to_watch": ["Keep ranked game"]}), encoding="utf-8")
    settings = Settings()
    assert type(settings.minimum_refresh_interval_minutes) is int
    assert settings.minimum_refresh_interval_minutes == 30
    assert settings.games_to_watch == ["Keep ranked game"]
    settings.save()
    assert Settings().minimum_refresh_interval_minutes == 30


@pytest.mark.parametrize("value", INVALID_INTERVALS)
def test_invalid_direct_save_does_not_touch_disk(managed_settings, value):
    state = managed_settings
    before = state.path.read_bytes()
    state.settings.minimum_refresh_interval_minutes = value
    with pytest.raises(ValueError):
        state.settings.save()
    assert state.path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (1, 30, 1440))
async def test_valid_interval_persists_and_broadcasts(managed_settings, value):
    state = managed_settings
    result = state.manager.update_settings({"minimum_refresh_interval_minutes": value})
    await asyncio.sleep(0)
    assert result["minimum_refresh_interval_minutes"] == value
    assert state.settings.minimum_refresh_interval_minutes == value
    assert Settings().minimum_refresh_interval_minutes == value
    state.broadcaster.emit.assert_awaited_once_with("settings_updated", result)
    state.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (1, 1440))
async def test_failed_save_restores_live_interval(managed_settings, monkeypatch, value):
    state = managed_settings
    failure = OSError("synthetic write failure")
    monkeypatch.setattr(state.settings, "save", MagicMock(side_effect=failure))
    with pytest.raises(OSError) as raised:
        state.manager.update_settings({"minimum_refresh_interval_minutes": value})
    await asyncio.sleep(0)
    assert raised.value is failure
    assert state.settings.minimum_refresh_interval_minutes == 30
    assert Settings().minimum_refresh_interval_minutes == 30
    state.callback.assert_not_called()
    state.broadcaster.emit.assert_not_called()
    state.console.print.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", ({}, {"minimum_refresh_interval_minutes": None}))
async def test_omitted_and_null_interval_leave_existing_value(managed_settings, updates):
    state = managed_settings
    state.settings.minimum_refresh_interval_minutes = 120
    result = state.manager.update_settings(SettingsUpdate(**updates).model_dump(exclude_unset=True))
    await asyncio.sleep(0)
    assert result["minimum_refresh_interval_minutes"] == 120
    assert Settings().minimum_refresh_interval_minutes == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (0, -1, 1441, True, "30"))
async def test_http_validation_is_redacted_and_does_not_persist(managed_settings, settings_http, value):
    state = managed_settings
    before = state.path.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=settings_http), base_url="http://miner.test") as client:
        response = await client.post("/api/settings", json={
            "minimum_refresh_interval_minutes": value,
            "telegram_bot_token": "SYNTHETIC-REDACTION-CANARY",
            "games_to_watch": ["Must not become live"],
        })
    await asyncio.sleep(0)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}
    assert "SYNTHETIC-REDACTION-CANARY" not in response.text
    assert state.path.read_bytes() == before
    assert state.settings.minimum_refresh_interval_minutes == 30
    assert state.settings.games_to_watch == []
    state.broadcaster.emit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", (1, 30, 1440))
async def test_http_valid_interval_round_trip(managed_settings, settings_http, value):
    state = managed_settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=settings_http), base_url="http://miner.test") as client:
        saved = await client.post("/api/settings", json={"minimum_refresh_interval_minutes": value})
        fetched = await client.get("/api/settings")
    await asyncio.sleep(0)
    assert saved.status_code == fetched.status_code == 200
    assert saved.json()["settings"]["minimum_refresh_interval_minutes"] == value
    assert fetched.json()["minimum_refresh_interval_minutes"] == value
    assert Settings().minimum_refresh_interval_minutes == value
    state.broadcaster.emit.assert_awaited_once()


def test_missing_saved_interval_retains_default(managed_settings):
    managed_settings.path.write_text("{}", encoding="utf-8")
    assert Settings().minimum_refresh_interval_minutes == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("saved,expected", ((0, 30), (-1, 30), (1441, 30), (1, 1), (30, 30), (1440, 1440)))
async def test_loaded_settings_maintain_real_reload_delay(managed_settings, monkeypatch, saved, expected):
    state = managed_settings
    state.path.write_text(json.dumps({"minimum_refresh_interval_minutes": saved}), encoding="utf-8")
    miner = SimpleNamespace(settings=Settings(), _mnt_triggers=deque(),
                            request_inventory_refresh=MagicMock(), change_state=MagicMock())

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    monkeypatch.setattr("src.services.maintenance.datetime", Clock)
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    with monkeypatch.context() as scoped:
        scoped.setattr("src.services.maintenance.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await MaintenanceService(miner).run_maintenance_task()
    sleep.assert_awaited_once_with(expected * 60)
    miner.request_inventory_refresh.assert_not_called()
    miner.change_state.assert_not_called()


def test_valid_boundaries_match_existing_dashboard_input():
    class Inputs(HTMLParser):
        attributes = None

        def handle_starttag(self, tag, attrs):
            if tag == "input" and dict(attrs).get("id") == "minimum-refresh-interval":
                self.attributes = dict(attrs)

    parser = Inputs()
    parser.feed((Path(__file__).resolve().parents[1] / "web/index.html").read_text(encoding="utf-8"))
    assert parser.attributes is not None
    assert SettingsUpdate(minimum_refresh_interval_minutes=int(parser.attributes["min"])).minimum_refresh_interval_minutes == 1
    assert SettingsUpdate(minimum_refresh_interval_minutes=int(parser.attributes["max"])).minimum_refresh_interval_minutes == 1440
