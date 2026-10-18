"""Displayed reports and conditional planning use the same direct evidence."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.web.managers.campaigns import CampaignProgressManager
from tests.javascript_helpers import NODE
from tests.test_deadline_channels import index
from tests.test_deadline_forecast import NOW
from tests.test_deadline_planner import HORIZON, assumptions, plan
from tests.test_deadline_planner import planning as planner_state
from tests.test_reported_progress_frontend import run_ui


pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for progress DOM tests")


@pytest.fixture
def progress_state(monkeypatch):
    return planner_state.__wrapped__(monkeypatch)


async def payload(drop, remaining=None):
    broadcaster = SimpleNamespace(emit=AsyncMock())
    manager = CampaignProgressManager(broadcaster)
    manager.update(drop, drop.remaining_minutes * 60 if remaining is None else remaining)
    await asyncio.sleep(0)
    initial = manager.get_current_drop()
    broadcaster.emit.assert_awaited_once_with("drop_progress", initial)
    return manager, initial


@pytest.mark.asyncio
@pytest.mark.parametrize("local_minutes", [0, 45, 119])
@pytest.mark.parametrize("evidence", ["fresh_zero", "missing", "stale_zero"])
async def test_local_counters_cannot_change_reported_minutes_or_planning_freshness(
    progress_state, local_minutes, evidence,
):
    state = progress_state
    twitch, _, watching, guard, _ = state[0]
    premises = assumptions(state)
    drop = state[1].timed_drops["urgent"]
    original = drop.progress_observation
    drop.real_current_minutes = local_minutes
    if evidence == "missing":
        drop._progress_observation = None
    elif evidence == "stale_zero":
        drop._progress_observation["received_at"] = (NOW - timedelta(minutes=6)).isoformat()
    reading = drop.progress_observation
    _, data = await payload(drop)
    assert data["current_minutes"] == local_minutes
    assert data["progress_observation"] == reading

    captured = guard.capture(twitch, now=NOW, horizon=HORIZON, enabled=True, include_channels=True)
    assert captured.reason == "captured"
    projected = next(item for item in captured.bound.inputs.drops if item.id == '["two","urgent"]')
    assert projected.observed_minutes == (None if evidence == "missing" else 0)
    expected = "No Twitch progress report" if evidence == "missing" else "Twitch report: 0 / 120 min"
    run_ui("const actual = " + json.dumps(data) + ";\nupdateDropProgress(actual);\n"
           + "assert.equal(element('progress-reported').textContent," + json.dumps(expected) + ");\n"
           + "assert.equal(element('progress-text').textContent," + json.dumps(f"Local: {local_minutes} / 120 min") + ");\n"
           + "assert.equal(element('progress-received').dateTime," + json.dumps(reading["received_at"] if reading else "") + ");\n"
           + "assert.equal(timers.length,0);\n")

    result = plan(state, premises)
    if evidence == "fresh_zero":
        assert reading == original
        assert result.reason == "rescue"
        assert result.certificate.target_id == projected.id
        assert result.certificate.slots[0].until == NOW + timedelta(hours=2, seconds=10)
    else:
        assert result.reason == "unknown_progress"
        assert result.certificate is None and result.valid_until is None
    assert drop.progress_observation == reading
    assert drop.real_current_minutes == local_minutes
    assert twitch.watching_channel.get_with_default(None) is watching
    assert twitch.settings.games_to_watch == ["High", "Urgent"]


@pytest.mark.asyncio
async def test_same_id_replacement_cannot_reuse_a_displayed_report(progress_state):
    state = progress_state
    twitch = state[0][0]
    old = state[1]
    drop = old.timed_drops["urgent"]
    premises = assumptions(state)
    manager, previous = await payload(drop)
    assert plan(state, premises).reason == "rescue"

    from tests.test_deadline_inputs import campaign
    from tests.test_drop_ignore_policy import _drop

    raw = _drop("urgent", "Urgent", required_minutes=120)
    replacement = campaign(twitch, "two", "Urgent", deadline=4, drops=[raw])
    assert replacement is not old and replacement.id == old.id
    twitch.inventory.remove(old)
    replacement_drop = replacement.timed_drops["urgent"]
    index(twitch)
    manager.update(replacement_drop, replacement_drop.remaining_minutes * 60)
    await asyncio.sleep(0)
    current = manager.get_current_drop()
    assert current["drop_id"] == previous["drop_id"]
    assert current["progress_observation"] is None
    assert previous["progress_observation"]["minutes"] == 0
    run_ui("updateDropProgress(" + json.dumps(previous) + ");\n"
           + "assert.equal(element('progress-received').hidden,false);\n"
           + "updateDropProgress(" + json.dumps(current) + ");\n"
           + "assert.equal(element('progress-reported').textContent,'No Twitch progress report');\n"
           + "assert.equal(element('progress-received').dateTime,'');\n"
           + "assert.equal(element('progress-received').hidden,true);\n"
           + "assert.equal(timers.length,0);\n")
    assert plan(state, premises).reason == "unknown_progress"
    assert replacement_drop.progress_observation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [-540, -1, 0])
async def test_overshot_local_progress_does_not_create_negative_time_or_fake_twitch_credit(
    progress_state, remaining,
):
    drop = progress_state[1].timed_drops["urgent"]
    drop.real_current_minutes = 119
    drop.extra_current_minutes = 10
    reading = drop.progress_observation
    manager, data = await payload(drop, remaining)
    expected_time = "0:00" if remaining == 0 else "--:--"
    run_ui("updateDropProgress(" + json.dumps(data) + ");\n"
           + "assert.equal(element('progress-text').textContent,'Local: 129 / 120 min');\n"
           + "assert.equal(element('progress-reported').textContent,'Twitch report: 0 / 120 min');\n"
           + "assert.equal(element('progress-time').textContent," + json.dumps(f"Estimated remaining: {expected_time}") + ");\n"
           + "assert.equal(timers.length,0);\nclearDropProgress();\n"
           + "assert.equal(element('progress-received').textContent,'');\n"
           + "assert.equal(element('progress-received').dateTime,'');\n")
    assert data["progress_observation"] == reading
    manager.stop_timer()
    await asyncio.sleep(0)
    assert manager.get_current_drop() is None
    assert drop.progress_observation == reading
