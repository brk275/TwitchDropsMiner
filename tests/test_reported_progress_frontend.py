"""Local counters and receipt metadata must not impersonate live Twitch credit."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.i18n.translator import GUIProgress
from src.web.managers.campaigns import CampaignProgressManager
from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function
from tests.test_progress_observation import make_campaign


ROOT = Path(__file__).resolve().parents[1]
KEYS = {"local_progress", "reported_progress", "no_reported_progress", "report_received", "estimated_remaining"}
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for progress DOM tests")


def run_ui(script):
    source = APP_JS.read_text()
    functions = [extract_javascript_function(source, name) for name in (
        "renderProgressSummary", "updateDropProgress", "updateRemainingTime", "clearDropProgress",
    ) if f"function {name}(" in source]
    setup = r"""
const assert = require('node:assert/strict');
const timers = [];
const cleared = [];
const state = {currentDrop:null, countdownTimer:null, translations:{}};
const elements = {};
function element(id) {
    return elements[id] ??= {textContent:'', hidden:false, dateTime:'', style:{}, children:[],
        replaceChildren(...children) { this.children = children; },
        removeAttribute(name) { if(name === 'datetime') this.dateTime = ''; },
    };
}
const document = {getElementById:element, createTextNode:text => ({textContent:text})};
function makeElement(tag, attrs, text) { return {tag, attrs, textContent:text}; }
function updateDropArt() {}
function setTimeout(fn, delay) { timers.push({fn,delay}); return timers.length; }
function clearTimeout(id) { cleared.push(id); }
const observation = {minutes:0, source:'websocket', received_at:'2026-10-18T04:00:00+00:00'};
const drop = {drop_id:'one',drop_name:'Reward',campaign_id:'campaign',campaign_name:'Campaign',
    game_name:'Game', current_minutes:20, required_minutes:60,progress:1/3,remaining_seconds:2400,
    progress_observation:observation};
"""
    result = subprocess.run([NODE, "-"], input=setup + "\n" + "\n".join(functions) + "\n" + script,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_progress_updates_do_not_start_a_browser_only_countdown():
    run_ui(r"""
updateDropProgress(drop);
assert.equal(timers.length, 0, 'A local timer must not imply unreported progress');
assert.ok(element('progress-time').textContent.includes('40:00'));
updateDropProgress({...drop,current_minutes:21,remaining_seconds:2340});
assert.ok(element('progress-time').textContent.includes('39:00'));
assert.equal(timers.length, 0);
""")


@pytest.mark.parametrize("source", ["inventory", "websocket", "current_drop"])
def test_zero_reading_remains_distinct_from_local_inferred_minutes(source):
    run_ui("observation.source = " + json.dumps(source) + r""";
const before = JSON.stringify(drop);
updateDropProgress(drop);
assert.match(element('progress-text').textContent, /Local.*20.*60/);
assert.match(element('progress-reported').textContent, /Twitch.*0.*60/);
assert.equal(element('progress-received').dateTime, observation.received_at);
assert.equal(element('progress-received').hidden, false);
assert.equal(JSON.stringify(drop), before);
assert.equal(timers.length, 0);
""")


@pytest.mark.parametrize("reading", [
    None, {}, [], {"minutes": 10},
    {"minutes": True, "source": "websocket", "received_at": "2026-10-18T04:00:00Z"},
    {"minutes": "10", "source": "websocket", "received_at": "2026-10-18T04:00:00Z"},
    {"minutes": -1, "source": "websocket", "received_at": "2026-10-18T04:00:00Z"},
    {"minutes": 61, "source": "websocket", "received_at": "2026-10-18T04:00:00Z"},
    {"minutes": 10, "source": "estimate", "received_at": "2026-10-18T04:00:00Z"},
    {"minutes": 10, "source": "websocket", "received_at": "2026-10-18T04:00:00"},
    {"minutes": 10, "source": "websocket", "received_at": "not a timestamp"},
    {"minutes": 10, "source": "websocket", "received_at": "2026-02-30T04:00:00Z"},
    {"minutes": 10, "source": "websocket", "received_at": "2026-10-18T24:00:00Z"},
    {"minutes": 10, "source": "websocket", "received_at": "2026-10-18T04:00:00+24:00"},
    {"minutes": 10, "source": "<img src=x onerror=alert(1)>", "received_at": "2026-10-18T04:00:00Z"},
])
def test_unknown_or_invalid_reading_never_becomes_confirmed_zero(reading):
    run_ui("updateDropProgress(drop); drop.progress_observation = " + json.dumps(reading) + r""";
updateDropProgress(drop);
assert.equal(element('progress-reported').textContent, 'No Twitch progress report');
assert.equal(element('progress-received').hidden, true);
assert.equal(element('progress-received').dateTime, '');
assert.equal(element('progress-received').textContent, '');
""")


def test_claim_normalization_does_not_relabel_an_old_reading_and_drop_change_clears_it():
    run_ui(r"""
observation.minutes = 5;
updateDropProgress({...drop,current_minutes:60,progress:1,remaining_seconds:0});
assert.match(element('progress-reported').textContent, /Twitch.*5.*60/);
assert.match(element('progress-text').textContent, /Local.*60.*60/);
updateDropProgress({...drop,drop_id:'another',progress_observation:null});
assert.equal(element('progress-reported').textContent, 'No Twitch progress report');
assert.equal(element('progress-received').dateTime, '');
clearDropProgress();
assert.equal(state.currentDrop,null);
assert.equal(element('drop-info').style.display,'none');
assert.equal(element('progress-received').textContent,'');
assert.equal(timers.length,0);
""")


@pytest.mark.parametrize("stamp", ["2026-10-18T04:00:00Z", "2026-10-18T04:00:00.1+00:00",
                                  "2026-10-18T04:00:00.123456+00:00", "2026-10-18T04:00:00+05:45"])
def test_valid_aware_receipts_keep_the_original_timestamp(stamp):
    run_ui("observation.received_at = " + json.dumps(stamp) + r""";
updateDropProgress(drop);
assert.equal(element('progress-received').hidden,false);
assert.equal(element('progress-received').dateTime,observation.received_at);
assert.match(element('progress-reported').textContent,/Twitch.*0.*60/);
""")


def test_all_translations_and_schema_include_progress_evidence_labels():
    assert set(GUIProgress.__annotations__) >= KEYS
    for path in (ROOT / "lang").glob("*.json"):
        progress = json.loads(path.read_text())["gui"]["progress"]
        assert set(progress) >= KEYS, path.name


def test_current_language_is_used_when_existing_drop_is_redrawn():
    english = json.loads((ROOT / "lang" / "English.json").read_text())
    german = json.loads((ROOT / "lang" / "Deutsch.json").read_text())
    run_ui("state.translations = " + json.dumps(english) + r""";
updateDropProgress(drop);
const first = element('progress-reported').textContent;
""" + "state.translations = " + json.dumps(german) + r""";
updateDropProgress(state.currentDrop);
assert.notEqual(element('progress-reported').textContent,first);
assert.equal(element('progress-received').dateTime,observation.received_at);
assert.equal(timers.length,0);
""")


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [None, 0, 9])
async def test_actual_progress_manager_payload_keeps_a_local_bump_separate_from_reported_minutes(minutes):
    campaign = make_campaign(minutes)
    drop = campaign.timed_drops["drop"]
    broadcaster = SimpleNamespace(emit=AsyncMock())
    manager = CampaignProgressManager(broadcaster)
    before = drop.progress_observation
    drop._bump_minutes(None)
    manager.update(drop, drop.remaining_minutes * 60)
    await asyncio.sleep(0)
    initial = manager.get_current_drop()
    event = broadcaster.emit.await_args.args
    assert event == ("drop_progress", initial)
    assert initial["progress_observation"] == before
    expected = ("No Twitch progress report" if minutes is None else f"Twitch report: {minutes} / 60 min")
    run_ui("const actual = " + json.dumps(initial) + ";\nupdateDropProgress(actual);\n"
           + "assert.equal(element('progress-reported').textContent," + json.dumps(expected) + ");\n"
           + "assert.equal(timers.length,0);\n"
           + "assert.ok(element('progress-text').textContent.includes(String(actual.current_minutes)));\n")
    assert drop.progress_observation == before
