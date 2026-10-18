from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.drop_history import HISTORY_VERSION, DropHistory


class _MockBenefit:
    def __init__(self, name: str) -> None:
        self.name = name


class _MockDrop:
    id = "drop-1"
    name = "Watch 30 Minutes"
    required_minutes = 30
    benefits = [_MockBenefit("Cool Badge"), _MockBenefit("Launch Emote")]

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(b.name for b in self.benefits)


class _MockGame:
    name = "Valorant"


class _MockCampaign:
    id = "camp-1"
    name = "Summer Drops"
    game = _MockGame()


class _MockBroadcasterDrop:
    """Drop whose benefits attribute raises, forcing the fallback path."""

    id = "drop-2"
    name = "Legacy Drop"
    required_minutes = 60

    @property
    def benefits(self):
        raise AttributeError("benefits missing")

    def rewards_text(self, delim: str = ", ") -> str:
        return "Legacy Reward"


class _MockBroadcasterCampaign:
    id = "camp-2"
    name = "Legacy Campaign"
    game = _MockGame()


def _fresh_history(tmp_path: Path) -> DropHistory:
    return DropHistory(tmp_path)


def test_record_and_total_count(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    assert history.total_count == 1

    # Duplicate drop IDs are ignored, even across reloads
    history.record(_MockDrop(), _MockCampaign())
    assert history.total_count == 1


def test_persistence_across_instances(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())

    reloaded = DropHistory(tmp_path)
    assert reloaded.total_count == 1
    assert reloaded.get_entries()[0]["drop_name"] == "Watch 30 Minutes"


def test_get_entries_filters(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())

    assert history.get_entries() == history.get_entries(game="VALORANT")
    assert history.get_entries(game="warframe") == []

    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert history.get_entries(since=since) != []
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert history.get_entries(since=future) == []

    assert len(history.get_entries(limit=1)) == 1


def test_since_filter_compares_instants_instead_of_iso_strings(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    history._entries[0]["claimed_at"] = "2026-02-03T02:30:00+00:00"
    offset = datetime.fromisoformat("2026-02-03T12:30:00+10:00")
    assert len(history.get_entries(since=offset)) == 1
    assert history.get_entries(since=offset + timedelta(microseconds=1)) == []
    assert len(history.get_entries(since=datetime(2026, 2, 3, 2, 30))) == 1


def test_benefits_fallback_when_benefits_attribute_missing(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockBroadcasterDrop(), _MockBroadcasterCampaign())

    entry = history.get_entries()[0]
    assert entry["benefits"] == ["Legacy Reward"]


def test_to_csv_headers_and_rows(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())

    csv_content = history.to_csv()
    lines = csv_content.strip().splitlines()

    assert lines[0].startswith("claimed_at,game,campaign,drop_name,benefits")
    row = lines[1]
    assert "Valorant" in row
    assert "Cool Badge; Launch Emote" in row
    assert "drop-1" in row


def test_stats_by_game_and_month(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    history.record(_MockBroadcasterDrop(), _MockBroadcasterCampaign())

    assert history.stats_by_game() == {"Valorant": 2}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert history.stats_by_month() == {month: 2}


def test_clear_deletes_entries_from_disk(tmp_path):
    history = _fresh_history(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    history.clear()
    assert history.total_count == 0

    reloaded = DropHistory(tmp_path)
    assert reloaded.total_count == 0


def test_unknown_version_file_is_ignored(tmp_path):
    payload = {"version": HISTORY_VERSION + 1, "entries": [{"id": "stale"}]}
    target = tmp_path / "drop_history.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    history = _fresh_history(tmp_path)
    assert history.total_count == 0


def test_corrupt_history_file_does_not_crash(tmp_path):
    (tmp_path / "drop_history.json").write_text("{not json", encoding="utf-8")

    history = _fresh_history(tmp_path)
    assert history.total_count == 0


@pytest.mark.parametrize("payload", [
    None, [], 1, "history", True,
    {"version": 1, "entries": None},
    {"version": 1, "entries": {}},
    {"version": 1, "entries": "entries"},
    {"version": True, "entries": []},
    {"version": 1.0, "entries": []},
])
def test_invalid_history_document_does_not_block_recording(tmp_path, payload):
    target = tmp_path / "drop_history.json"
    original = json.dumps(payload)
    target.write_text(original, encoding="utf-8")

    history = DropHistory(tmp_path)

    assert history.total_count == 0
    assert target.read_text(encoding="utf-8") == original
    history.record(_MockDrop(), _MockCampaign())
    assert DropHistory(tmp_path).total_count == 1


@pytest.mark.parametrize("bad_row", [
    None, [], "entry", 1, {},
    {"campaign_id": None},
    {"game": None},
    {"benefits": "reward"},
    {"benefits": [None]},
    {"required_minutes": True},
    {"required_minutes": -1},
    {"required_minutes": 1.5},
    {"claimed_at": "not a date"},
    {"claimed_at": "2026-01-01"},
    {"claimed_at": "2026-01-01T00:00:00"},
    {"claimed_at": "0001-01-01T00:00:00+01:00"},
])
def test_bad_history_rows_do_not_poison_valid_rows(tmp_path, bad_row):
    history = DropHistory(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    valid = history.get_entries()[0]
    invalid = {**valid, **bad_row} if isinstance(bad_row, dict) and bad_row else bad_row
    target = tmp_path / "drop_history.json"
    original = json.dumps({"version": HISTORY_VERSION, "entries": [valid, invalid]})
    target.write_text(original, encoding="utf-8")

    loaded = DropHistory(tmp_path)

    assert loaded.get_entries() == [valid]
    assert loaded.get_entries(game="VALORANT", campaign_id="camp-1") == [valid]
    assert loaded.get_entries(since=datetime(2000, 1, 1, tzinfo=timezone.utc)) == [valid]
    assert loaded.stats_by_game() == {"Valorant": 1}
    assert sum(loaded.stats_by_month().values()) == 1
    assert len(loaded.to_csv().splitlines()) == 2
    assert target.read_text(encoding="utf-8") == original
    loaded.record(_MockDrop(), _MockCampaign())
    assert loaded.total_count == 1
    loaded.record(_MockBroadcasterDrop(), _MockBroadcasterCampaign())
    assert DropHistory(tmp_path).total_count == 2


def test_loaded_claim_timestamp_is_normalized_to_utc(tmp_path):
    history = DropHistory(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    entry = history.get_entries()[0]
    entry["claimed_at"] = "2026-03-01T00:30:00+01:00"
    entry["required_minutes"] = 0
    entry["benefits"] = []
    target = tmp_path / "drop_history.json"
    target.write_text(json.dumps({"version": HISTORY_VERSION, "entries": [entry]}))

    loaded = DropHistory(tmp_path)

    assert loaded.get_entries()[0]["claimed_at"] == "2026-02-28T23:30:00+00:00"
    assert loaded.stats_by_month() == {"2026-02": 1}
    assert loaded.get_entries(since=datetime(2026, 3, 1, tzinfo=timezone.utc)) == []
    assert loaded.get_entries()[0]["required_minutes"] == 0


def test_invalid_utf8_history_does_not_block_startup(tmp_path, caplog):
    target = tmp_path / "drop_history.json"
    original = b'\xffprivate-history-marker'
    target.write_bytes(original)

    history = DropHistory(tmp_path)

    assert history.total_count == 0
    assert target.read_bytes() == original
    assert "private-history-marker" not in caplog.text


@pytest.mark.parametrize("field", [
    "id", "claimed_at", "game", "campaign", "drop_name", "campaign_id",
    "benefits", "required_minutes",
])
@pytest.mark.parametrize("mutation", ["missing", "null", "object"])
def test_each_required_history_field_is_validated(tmp_path, field, mutation):
    history = DropHistory(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    entry = history.get_entries()[0]
    if mutation == "missing":
        del entry[field]
    else:
        entry[field] = None if mutation == "null" else {}
    target = tmp_path / "drop_history.json"
    target.write_text(json.dumps({"version": HISTORY_VERSION, "entries": [entry]}))

    assert DropHistory(tmp_path).get_entries() == []


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2])
def test_non_integer_or_unknown_version_does_not_accept_rows(tmp_path, version):
    history = DropHistory(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    target = tmp_path / "drop_history.json"
    target.write_text(json.dumps({"version": version, "entries": history.get_entries()}))

    assert DropHistory(tmp_path).get_entries() == []


def test_loading_retains_insertion_order_and_redacts_rejected_values(tmp_path, caplog):
    history = DropHistory(tmp_path)
    history.record(_MockDrop(), _MockCampaign())
    first = history.get_entries()[0]
    second = {**first, "id": "drop-2", "claimed_at": "2020-01-01T00:00:00Z"}
    target = tmp_path / "drop_history.json"
    target.write_text(json.dumps({"version": HISTORY_VERSION, "entries": [
        first, {"id": "private-history-marker"}, second,
    ]}))

    loaded = DropHistory(tmp_path)

    assert [e["id"] for e in loaded.get_entries()] == ["drop-2", "drop-1"]
    assert "Ignored 1 invalid history entries." in caplog.text
    assert "private-history-marker" not in caplog.text


def test_history_read_failure_is_redacted_and_does_not_block_startup(tmp_path, monkeypatch, caplog):
    def fail_open(*args, **kwargs):
        raise OSError("private-history-marker")

    monkeypatch.setattr(Path, "open", fail_open)

    assert DropHistory(tmp_path).total_count == 0
    assert "Failed to load drop history" in caplog.text
    assert "private-history-marker" not in caplog.text


def test_nested_history_document_does_not_block_startup(tmp_path):
    target = tmp_path / "drop_history.json"
    original = "[" * 2000 + "0" + "]" * 2000
    target.write_text(original)

    assert DropHistory(tmp_path).total_count == 0
    assert target.read_text() == original
