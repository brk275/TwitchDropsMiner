"""Reconnect channel snapshots replace rows missed while disconnected."""

import json
import subprocess

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function
from tests.test_stream_preview import HARNESS


pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for frontend behavior tests")


def run_snapshot_script(assertions):
    source = APP_JS.read_text(encoding="utf-8")
    handlers = source[source.index("socket.on('initial_state'"):source.index("socket.on('drop_progress'")]
    functions = "\n".join(extract_javascript_function(source, name) for name in (
        "updateChannel", "removeChannel", "clearChannels", "setWatchingChannel",
        "clearWatchingChannel", "updateNowWatching",
    ))
    script = HARNESS + """
const handlers = {}, socket = {on(name, callback) {handlers[name] = callback;}};
const state = {channels: {}, translations: {}, previewEnabled: false};
function renderChannels() {updateNowWatching();}
function clearDropProgress() {}
""" + functions + handlers + """
const old = {id:7,name:'Old',login:'old',watching:true,viewers:10};
const fresh = {id:8,name:'Fresh',login:'fresh',watching:true,viewers:20};
handlers.initial_state({channels:[old]});
""" + assertions
    result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rows, expected", [
    ([{"id": 8, "name": "Fresh", "login": "fresh", "watching": True}], [8]),
    ([], []),
    ([{"id": 8, "name": "Fresh", "login": "fresh", "watching": False}], []),
    ([{"id": 7, "name": "Old", "login": "old", "watching": False},
      {"id": 8, "name": "Fresh", "login": "fresh", "watching": True}], [8]),
])
def test_authoritative_snapshot_removes_old_rows_and_old_watch_flags(rows, expected):
    run_snapshot_script("""
const rows = ROWS;
handlers.initial_state({channels:rows});
assert.deepEqual(Object.keys(state.channels).sort(),rows.map(row=>String(row.id)).sort());
assert.deepEqual(Object.values(state.channels).filter(row=>row.watching).map(row=>row.id),EXPECTED);
assert.equal(document.getElementById('now-watching').classList.contains('hidden'),EXPECTED.length===0);
if(EXPECTED.length) assert.match(document.getElementById('now-watching-info').textContent,/^Fresh/);
assert.equal(state.previewEnabled,false);
assert.equal(document.getElementById('now-watching-img').style.backgroundImage,'');
""".replace("ROWS", json.dumps(rows)).replace("EXPECTED", json.dumps(expected)))


@pytest.mark.parametrize("payload", [{}, {"channels": None}])
def test_omitted_or_null_channel_snapshot_keeps_existing_partial_event_behavior(payload):
    run_snapshot_script("""
handlers.initial_state(PAYLOAD);
assert.deepEqual(Object.keys(state.channels),['7']);
assert.equal(state.channels[7].watching,true);
""".replace("PAYLOAD", json.dumps(payload)))


def test_incremental_events_keep_working_after_snapshot_replacement():
    run_snapshot_script("""
handlers.initial_state({channels:[fresh]});
handlers.channel_add({id:9,name:'New',login:'new',watching:false});
handlers.channel_watching({id:9});
assert.deepEqual(Object.keys(state.channels),['8','9']);
assert.equal(state.channels[8].watching,false);
assert.equal(state.channels[9].watching,true);
assert.match(document.getElementById('now-watching-info').textContent,/^New/);
handlers.channel_remove({id:9});
assert.equal(document.getElementById('now-watching').classList.contains('hidden'),true);
""")
