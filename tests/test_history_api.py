from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import pytest
from fastapi.responses import JSONResponse

from src.drop_history import DropHistory


web_app = importlib.import_module("src.web.app")


class _FakeHistory:
    def __init__(self) -> None:
        self.total_count = 2

    def get_entries(self, **kwargs):
        return [{"id": "1", "claimed_at": "2026-01-01T00:00:00+00:00", "game": "Valorant"}]

    def to_csv(self, **kwargs):
        return "claimed_at,game\n2026-01-01T00:00:00+00:00,Valorant\n"

    def stats_by_game(self):
        return {"Valorant": 2}

    def stats_by_month(self):
        return {"2026-01": 2}

    def clear(self):
        self.total_count = 0


def _fake_client() -> SimpleNamespace:
    return SimpleNamespace(drop_history=_FakeHistory())


@pytest.mark.asyncio
async def test_history_endpoint_returns_entries():
    with patch.object(web_app, "twitch_client", _fake_client()):
        response = await web_app.get_history(game="Valorant", since="2026-01-01", limit=50)

    assert response["total"] == 2
    assert response["entries"][0]["game"] == "Valorant"


@pytest.mark.asyncio
async def test_history_endpoint_requires_client():
    with patch.object(web_app, "twitch_client", None):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await web_app.get_history()
        assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_export_csv_includes_bom_and_filename():
    with patch.object(web_app, "twitch_client", _fake_client()):
        response = await web_app.export_history_csv(game="Valorant")

    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert "filename*=UTF-8''drop_history_Valorant.csv" in response.headers["content-disposition"]
    assert response.body.startswith("\ufeff".encode("utf-8"))
    assert "Valorant" in response.body.decode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("game", ["原神", 'A "game"\r\nInjected: value', "Baldur's Gate 3"])
async def test_export_csv_encodes_untrusted_unicode_filename(game):
    with patch.object(web_app, "twitch_client", _fake_client()):
        response = await web_app.export_history_csv(game=game)
    disposition = response.headers["content-disposition"]
    assert 'filename="drop_history.csv"' in disposition
    assert "filename*=UTF-8''" + quote(f"drop_history_{game}.csv", safe="") in disposition
    assert "\r" not in disposition and "\n" not in disposition
    disposition.encode("ascii")


@pytest.mark.asyncio
async def test_stats_endpoint_aggregates():
    with patch.object(web_app, "twitch_client", _fake_client()):
        response = await web_app.get_history_stats()

    assert response == {"total_drops": 2, "by_game": {"Valorant": 2}, "by_month": {"2026-01": 2}}


@pytest.mark.asyncio
async def test_delete_history_endpoint_clears():
    client = _fake_client()
    with patch.object(web_app, "twitch_client", client):
        response = await web_app.clear_history()

    assert response == {"success": True}
    assert client.drop_history.total_count == 0


def test_parse_history_since_accepts_date_and_datetime():
    assert web_app._parse_history_since("2026-02-03") is not None
    assert web_app._parse_history_since("2026-02-03T12:30:00+00:00") is not None
    assert web_app._parse_history_since(None) is None
    assert web_app._parse_history_since("not-a-date") is None


@pytest.mark.parametrize("value", [
    "2026-02-03T12:30:00+10:00", "2026-02-02T21:30:00-05:00",
    "2026-02-03T02:30:00Z", "2026-02-03 02:30:00", "2026-02-03T02:30:00",
])
def test_history_since_preserves_instant_and_normalizes_to_utc(value):
    assert web_app._parse_history_since(value) == datetime(2026, 2, 3, 2, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_loaded_history_projects_validated_fields_before_json_response(tmp_path):
    entry = {
        "id": "drop-1", "claimed_at": "2026-01-01T00:00:00+00:00", "game": "Game",
        "campaign": "Campaign", "drop_name": "Reward", "benefits": [],
        "required_minutes": 0, "campaign_id": "campaign-1",
    }
    raw = {**entry, "extra": float("nan"), "private": "private-history-marker"}
    target = tmp_path / "drop_history.json"
    original = json.dumps({"version": 1, "entries": [None, raw]})
    target.write_text(original)
    client = SimpleNamespace(drop_history=DropHistory(tmp_path))

    with patch.object(web_app, "twitch_client", client):
        response = JSONResponse(await web_app.get_history(game="Game", since="2026-01-01"))
        stats = JSONResponse(await web_app.get_history_stats())
        csv_response = await web_app.export_history_csv(game="Game")

    assert json.loads(response.body) == {"total": 1, "entries": [entry]}
    assert json.loads(stats.body) == {
        "total_drops": 1, "by_game": {"Game": 1}, "by_month": {"2026-01": 1},
    }
    assert "Reward" in csv_response.body.decode("utf-8")
    assert "private-history-marker" not in response.body.decode("utf-8")
    assert target.read_text() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "id", "game", "campaign", "drop_name", "campaign_id", "benefits",
])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
async def test_history_rejects_escaped_surrogates_without_breaking_claim_save(
    tmp_path, field, surrogate,
):
    reward = "\u5956\u54c1 \U0001f680"
    valid = {
        "id": "one", "claimed_at": "2026-01-01T00:00:00+00:00", "game": "Game",
        "campaign": "Campaign", "drop_name": reward, "benefits": [reward],
        "required_minutes": 0, "campaign_id": "campaign-1",
    }
    malformed = {**valid, field: [surrogate] if field == "benefits" else surrogate}
    target = tmp_path / "drop_history.json"
    original = json.dumps({"version": 1, "entries": [valid, malformed]})
    target.write_text(original, encoding="utf-8")
    history = DropHistory(tmp_path)
    assert history.get_entries() == [valid]
    assert target.read_text(encoding="utf-8") == original

    history.record(
        SimpleNamespace(id="two", name=reward, benefits=[], required_minutes=30),
        SimpleNamespace(id="campaign-1", name="Campaign", game=SimpleNamespace(name="Game")),
    )
    loaded = DropHistory(tmp_path)
    assert loaded.total_count == 2
    assert loaded.get_entries()[1] == valid
    with patch.object(web_app, "twitch_client", SimpleNamespace(drop_history=loaded)):
        csv_response = await web_app.export_history_csv()
        response = JSONResponse(await web_app.get_history())
    assert reward in csv_response.body.decode("utf-8")
    assert reward in response.body.decode("utf-8")
