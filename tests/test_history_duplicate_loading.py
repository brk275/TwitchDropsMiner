"""Persisted duplicate IDs must not inflate confirmed local claim history."""

import csv
import importlib
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.drop_history import HISTORY_FILENAME, HISTORY_VERSION, DropHistory


def entry(drop_id="reward"):
    return {
        "id": drop_id, "claimed_at": "2026-10-18T04:00:00+00:00",
        "game": "Game", "campaign": "Campaign", "drop_name": "Reward",
        "benefits": ["Benefit"], "required_minutes": 30, "campaign_id": "campaign",
    }


def saved_history(tmp_path, rows):
    target = tmp_path / HISTORY_FILENAME
    original = json.dumps({"version": HISTORY_VERSION, "entries": rows})
    target.write_text(original, encoding="utf-8")
    return DropHistory(tmp_path), target, original


@pytest.mark.parametrize("copies", [2, 3, 100])
def test_identical_saved_duplicates_count_once_without_rewriting(tmp_path, copies):
    first = entry()
    history, target, original = saved_history(tmp_path, [first] * copies)

    assert history.get_entries() == [first]
    assert history.total_count == 1
    assert history.stats_by_game() == {"Game": 1}
    assert history.stats_by_month() == {"2026-10": 1}
    assert len(list(csv.DictReader(io.StringIO(history.to_csv())))) == 1
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("conflict", [
    {"claimed_at": "2020-01-01T00:00:00+00:00"},
    {"claimed_at": "2030-01-01T00:00:00+00:00"},
    {"game": "Other game", "campaign_id": "other-campaign"},
    {"benefits": ["Other benefit"], "drop_name": "Other reward"},
    {"required_minutes": 0},
])
def test_first_valid_record_wins_without_reordering_other_claims(tmp_path, conflict):
    first, second = entry(), entry("second")
    history, target, original = saved_history(
        tmp_path, [first, second, {**first, **conflict}],
    )

    assert history.get_entries() == [second, first]
    assert history.get_entries(campaign_id="campaign", limit=1) == [second]
    assert history.get_entries(since=datetime(2026, 10, 18, 4, tzinfo=timezone.utc)) == [second, first]
    assert history.stats_by_game() == {"Game": 2}
    assert history.stats_by_month() == {"2026-10": 2}
    assert target.read_text(encoding="utf-8") == original


def test_invalid_row_cannot_reserve_a_valid_later_id(tmp_path):
    valid = entry()
    history, _, _ = saved_history(tmp_path, [
        {**valid, "claimed_at": "invalid"}, valid, {**valid, "game": "Other"},
    ])
    assert history.get_entries() == [valid]


def test_exact_opaque_ids_remain_distinct_despite_equal_reward_names(tmp_path):
    rows = [entry(value) for value in ("reward", "Reward", " reward", "reward ")]
    history, _, _ = saved_history(tmp_path, rows)
    assert history.get_entries() == list(reversed(rows))
    assert history.total_count == 4


def test_replaying_existing_claim_does_not_rewrite_source_file(tmp_path):
    first = entry()
    history, target, original = saved_history(tmp_path, [first, first])
    history.record(
        SimpleNamespace(id="reward", name="Reward", benefits=[], required_minutes=30),
        SimpleNamespace(id="campaign", name="Campaign", game=SimpleNamespace(name="Game")),
    )
    assert history.get_entries() == [first]
    assert target.read_text(encoding="utf-8") == original


def test_next_successful_claim_persists_unique_history_and_survives_restart(tmp_path):
    first = entry()
    history, target, _ = saved_history(tmp_path, [first, first])
    history.record(
        SimpleNamespace(id="new", name="New reward", benefits=[], required_minutes=0),
        SimpleNamespace(id="campaign", name="Campaign", game=SimpleNamespace(name="Game")),
    )

    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert [row["id"] for row in persisted["entries"]] == ["reward", "new"]
    restarted = DropHistory(tmp_path)
    assert restarted.total_count == 2
    assert restarted.get_entries() == history.get_entries()


def test_duplicate_and_invalid_counts_are_separate_and_redacted(tmp_path, caplog):
    first = entry("private-history-marker")
    history, _, _ = saved_history(tmp_path, [None, first, first, {"id": "secret-marker"}])
    assert history.total_count == 1
    assert "Ignored 2 invalid history entries." in caplog.text
    assert "Ignored 1 duplicate history entries." in caplog.text
    assert "private-history-marker" not in caplog.text
    assert "secret-marker" not in caplog.text


@pytest.mark.asyncio
async def test_history_api_stats_and_csv_share_unique_loaded_records(tmp_path, monkeypatch):
    first, second = entry(), {**entry("second"), "game": "Second game"}
    history, target, original = saved_history(tmp_path, [first, second, first])
    app = importlib.import_module("src.web.app")
    monkeypatch.setattr(app, "twitch_client", SimpleNamespace(drop_history=history))

    result = await app.get_history(game="Game", since="2026-10-18", limit=10)
    assert result == {"total": 2, "entries": [first]}
    assert await app.get_history_stats() == {
        "total_drops": 2, "by_game": {"Game": 1, "Second game": 1},
        "by_month": {"2026-10": 2},
    }
    response = await app.export_history_csv()
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert [row["drop_id"] for row in rows] == ["second", "reward"]
    assert target.read_text(encoding="utf-8") == original
