"""Refresh timers use the confirmed interval after real atomic-save failures."""

import asyncio
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.config.settings import Settings
from src.services.maintenance import MaintenanceService
from src.utils import json_utils
from tests.test_refresh_interval import managed_settings as managed_settings
from tests.test_refresh_interval import settings_http as settings_http


async def next_delay(settings, monkeypatch):
    miner = SimpleNamespace(
        settings=settings, _mnt_triggers=deque(),
        request_inventory_refresh=MagicMock(), change_state=MagicMock(),
    )

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    with monkeypatch.context() as scoped:
        scoped.setattr("src.services.maintenance.datetime", Clock)
        scoped.setattr("src.services.maintenance.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await MaintenanceService(miner).run_maintenance_task()
    miner.request_inventory_refresh.assert_not_called()
    miner.change_state.assert_not_called()
    return sleep.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 1440])
@pytest.mark.parametrize("stage", ["dump", "fsync", "replace"])
async def test_failed_interval_publication_preserves_real_maintenance_delay(
    managed_settings, settings_http, monkeypatch, value, stage,
):
    state = managed_settings
    before = state.path.read_bytes()
    failure = OSError("synthetic-private-storage-detail")

    def partial_dump(contents, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        raise failure

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings_http), base_url="http://miner.test",
    ) as client:
        with monkeypatch.context() as fault:
            if stage == "dump":
                fault.setattr(json_utils.json, "dump", partial_dump)
            else:
                fault.setattr(json_utils.os, stage, MagicMock(side_effect=failure))
            response = await client.post("/api/settings", json={
                "minimum_refresh_interval_minutes": value,
            })
        await asyncio.sleep(0)
        assert response.status_code == 500
        assert response.json() == {"detail": "settings_save_failed"}
        assert state.path.read_bytes() == before
        assert set(state.path.parent.iterdir()) == {state.path}
        assert state.settings.minimum_refresh_interval_minutes == 30
        assert Settings().minimum_refresh_interval_minutes == 30
        state.console.print.assert_not_called()
        state.broadcaster.emit.assert_not_called()
        state.callback.assert_not_called()
        assert await next_delay(state.settings, monkeypatch) == 1800

        saved = await client.post("/api/settings", json={
            "minimum_refresh_interval_minutes": value,
        })
        fetched = await client.get("/api/settings")
    await asyncio.sleep(0)
    assert saved.status_code == fetched.status_code == 200
    assert saved.json()["settings"]["minimum_refresh_interval_minutes"] == value
    assert fetched.json()["minimum_refresh_interval_minutes"] == value
    assert Settings().minimum_refresh_interval_minutes == value
    assert await next_delay(state.settings, monkeypatch) == value * 60
    state.broadcaster.emit.assert_awaited_once()
    state.console.print.assert_called_once()
    # A setting change affects the next maintenance cycle, not an immediate reload.
    state.callback.assert_not_called()
