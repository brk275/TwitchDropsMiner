"""History deletion reports persistence failure without losing visible records."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from src.drop_history import DropHistory
from src.web.auth import AuthMiddleware, WebAuth


web = importlib.import_module("src.web.app")


def record(history, drop_id="one"):
    history.record(
        SimpleNamespace(id=drop_id, name="Reward", benefits=[], required_minutes=30),
        SimpleNamespace(id="campaign", name="Campaign", game=SimpleNamespace(name="Game")),
    )


@pytest.fixture
def history(tmp_path):
    history = DropHistory(tmp_path)
    record(history)
    return history


@pytest.mark.parametrize(("target", "method"), [
    (Path, "mkdir"), (Path, "open"), (json, "dump"), (Path, "replace"),
])
def test_failed_deletion_retains_memory_and_original_file(history, tmp_path, caplog, target, method):
    entries = history.get_entries()
    original = (tmp_path / "drop_history.json").read_bytes()
    with patch.object(target, method, side_effect=OSError("private-storage-marker")):
        assert history.clear() is False

    assert history.get_entries() == entries
    assert history.stats_by_game() == {"Game": 1}
    assert (tmp_path / "drop_history.json").read_bytes() == original
    assert DropHistory(tmp_path).get_entries() == entries
    assert "Drop history cleared." not in caplog.text
    assert "private-storage-marker" not in caplog.text


def test_history_deletion_can_retry_after_storage_recovers(history, tmp_path):
    with patch.object(Path, "replace", side_effect=OSError("storage unavailable")):
        assert history.clear() is False
    record(history, "two")
    assert DropHistory(tmp_path).total_count == 2

    assert history.clear() is True
    assert history.total_count == 0
    assert DropHistory(tmp_path).total_count == 0


def test_recording_remains_best_effort_when_storage_fails(history, tmp_path, caplog):
    with patch.object(Path, "replace", side_effect=OSError("private-storage-marker")):
        record(history, "two")

    assert history.total_count == 2
    assert DropHistory(tmp_path).total_count == 1
    assert "private-storage-marker" not in caplog.text


def test_empty_history_still_requires_a_successful_write(tmp_path):
    history = DropHistory(tmp_path)
    with patch.object(Path, "replace", side_effect=OSError("storage unavailable")):
        assert history.clear() is False
    assert history.total_count == 0
    assert not (tmp_path / "drop_history.json").exists()
    assert history.clear() is True
    assert json.loads((tmp_path / "drop_history.json").read_text())["entries"] == []


@pytest.mark.asyncio
async def test_delete_endpoint_preserves_history_on_failure_and_succeeds_on_retry(history, tmp_path):
    app = FastAPI()
    app.add_api_route("/api/history", web.clear_history, methods=["DELETE"])
    app.add_middleware(AuthMiddleware, auth=WebAuth(tmp_path / "auth.json"))
    with patch.object(web, "twitch_client", SimpleNamespace(drop_history=history)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            headers={"X-TDM-Request": "1"},
        ) as client:
            with patch.object(Path, "replace", side_effect=OSError("private-storage-marker")):
                response = await client.delete("/api/history")
            assert response.status_code == 503
            assert response.json() == {"detail": "Failed to clear drop history"}
            assert history.total_count == DropHistory(tmp_path).total_count == 1
            response = await client.delete("/api/history")
    assert response.status_code == 200 and response.json() == {"success": True}
    assert history.total_count == DropHistory(tmp_path).total_count == 0


@pytest.mark.asyncio
async def test_unauthorized_history_deletion_does_not_touch_storage(history, tmp_path):
    auth = WebAuth(tmp_path / "auth.json")
    auth.password_hash = "configured"
    app = FastAPI()
    app.add_api_route("/api/history", web.clear_history, methods=["DELETE"])
    app.add_middleware(AuthMiddleware, auth=auth)
    with patch.object(web, "twitch_client", SimpleNamespace(drop_history=history)), patch.object(
        history, "clear",
    ) as clear:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            headers={"X-TDM-Request": "1"},
        ) as client:
            assert (await client.delete("/api/history")).status_code == 401
            response = await client.delete("/api/history", headers={"Origin": "https://elsewhere.test"})
            assert response.status_code == 403
            clear.assert_not_called()
    assert history.total_count == DropHistory(tmp_path).total_count == 1
