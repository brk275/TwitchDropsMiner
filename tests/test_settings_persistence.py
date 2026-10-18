"""Failed settings writes must preserve the last complete configuration."""

import asyncio
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from src.config.settings import Settings
from src.utils import json_utils
from src.web.managers.settings import SettingsManager


@pytest.fixture
def saved_settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("src.config.settings.SETTINGS_PATH", path)
    settings = Settings()
    settings.games_to_watch = ["Keep ranked game"]
    settings.save()
    return settings, path


@pytest.mark.parametrize("existing", (False, True))
def test_partial_write_preserves_complete_destination(tmp_path, monkeypatch, existing):
    path = tmp_path / "settings.json"
    if existing:
        json_utils.json_save(path, {"keep": "previous complete settings"})
    before = path.read_bytes() if existing else None
    entries = set(tmp_path.iterdir())
    failure = OSError("synthetic partial write")

    def partial_dump(contents, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        raise failure

    monkeypatch.setattr(json_utils.json, "dump", partial_dump)
    with pytest.raises(OSError) as raised:
        json_utils.json_save(path, {"new": "settings"})
    assert raised.value is failure
    if existing:
        assert path.read_bytes() == before
    else:
        assert not path.exists()
    assert set(tmp_path.iterdir()) == entries


def test_serialization_error_preserves_destination(saved_settings):
    settings, path = saved_settings
    before = path.read_bytes()
    unsupported = object()
    with pytest.raises(TypeError) as raised:
        json_utils.json_save(path, {"unsupported": unsupported}, sort=True)
    assert raised.value.args[0] is unsupported
    assert path.read_bytes() == before
    assert Settings().games_to_watch == settings.games_to_watch


@pytest.mark.parametrize("operation", ("fsync", "replace"))
def test_prepublication_failure_preserves_settings(saved_settings, monkeypatch, operation):
    settings, path = saved_settings
    before, entries = path.read_bytes(), set(path.parent.iterdir())
    settings.games_to_watch = ["New ranked game"]
    failure = OSError("synthetic publication failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(os, operation, MagicMock(side_effect=failure))
        with pytest.raises(OSError) as raised:
            settings.save()
    assert raised.value is failure
    assert path.read_bytes() == before
    assert Settings().games_to_watch == ["Keep ranked game"]
    assert set(path.parent.iterdir()) == entries


@pytest.mark.asyncio
async def test_actual_manager_failure_keeps_disk_and_live_policy(saved_settings, monkeypatch):
    settings, path = saved_settings
    before = path.read_bytes()
    broadcaster, callback, console = AsyncMock(), MagicMock(), MagicMock()
    manager = SettingsManager(broadcaster, settings, console, on_change=callback)

    def partial_dump(contents, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError("synthetic disk full")

    monkeypatch.setattr(json_utils.json, "dump", partial_dump)
    with pytest.raises(OSError):
        manager.update_settings({"priority_list_only": False})
    await asyncio.sleep(0)
    assert settings.priority_list_only is True
    assert path.read_bytes() == before
    assert Settings().priority_list_only is True
    callback.assert_not_called()
    broadcaster.emit.assert_not_called()
    console.print.assert_not_called()


def test_readers_keep_complete_json_during_a_staged_write(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    old, new = {"value": "old complete settings"}, {"value": "new complete settings"}
    json_utils.json_save(path, old)
    entered, release = threading.Event(), threading.Event()

    def blocked_dump(contents, stream, **kwargs):
        encoded = json.dumps(contents, **kwargs)
        split = len(encoded) // 2
        stream.write(encoded[:split])
        stream.flush()
        entered.set()
        assert release.wait(10), "test writer was not released"
        stream.write(encoded[split:])

    monkeypatch.setattr(json_utils.json, "dump", blocked_dump)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(json_utils.json_save, path, new)
        try:
            assert entered.wait(10), "test writer never started"
            assert json.loads(path.read_text(encoding="utf-8")) == old
        finally:
            release.set()
            writer.result(timeout=10)
    assert json.loads(path.read_text(encoding="utf-8")) == new
    assert set(tmp_path.iterdir()) == {path}


def test_round_trip_preserves_serializer_and_format(tmp_path):
    path = tmp_path / "settings.json"
    values = {"z": "caf\u00e9", "a": {"url": URL("https://example.test/"),
              "at": datetime(2026, 10, 18, tzinfo=timezone.utc), "items": {"one", "two"}}}
    expected = json.dumps(values, default=json_utils._serialize, sort_keys=True, indent=4)
    json_utils.json_save(path, values, sort=True)
    assert path.read_text(encoding="utf-8") == expected
    assert json_utils.json_load(path, {}, merge=False) == values
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes do not establish Windows ACL privacy")
def test_settings_file_is_private_after_creation_and_replacement(tmp_path):
    path = tmp_path / "settings.json"
    for value in ("first", "replacement"):
        if path.exists():
            path.chmod(0o644)
        json_utils.json_save(path, {"synthetic_secret": value})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("existing", (False, True))
def test_symlink_keeps_its_target_and_identity(tmp_path, existing):
    target_dir, link_dir = tmp_path / "target", tmp_path / "links"
    target_dir.mkdir()
    link_dir.mkdir()
    target = target_dir / "settings.json"
    if existing:
        json_utils.json_save(target, {"value": "old"})
    link = link_dir / "settings.json"
    try:
        link.symlink_to(Path("..") / "target" / "settings.json")
    except OSError:
        if os.name == "nt":
            pytest.skip("Symbolic links require privileges on this Windows host")
        raise
    original_link = link.readlink()
    json_utils.json_save(link, {"value": "new"})
    assert link.is_symlink()
    assert link.readlink() == original_link
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "new"}
    assert set(target_dir.iterdir()) == {target}
    assert set(link_dir.iterdir()) == {link}


def test_missing_parent_is_not_created(tmp_path):
    path = tmp_path / "missing" / "settings.json"
    with pytest.raises(FileNotFoundError):
        json_utils.json_save(path, {})
    assert not path.parent.exists()


def test_directory_destination_is_not_modified(tmp_path):
    path = tmp_path / "settings.json"
    path.mkdir()
    canary = path / "keep.txt"
    canary.write_text("keep", encoding="utf-8")
    with pytest.raises(OSError):
        json_utils.json_save(path, {})
    assert canary.read_text(encoding="utf-8") == "keep"
    assert set(tmp_path.iterdir()) == {path}
