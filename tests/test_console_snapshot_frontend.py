"""Full console snapshots replace display history without deduplicating real lines."""

import json
import subprocess

import pytest

from tests.javascript_helpers import APP_JS, NODE, extract_javascript_function


pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for frontend behavior tests")


def run_console_script(assertions):
    source = APP_JS.read_text(encoding="utf-8")
    handlers = source[source.index("socket.on('initial_state'"):source.index("// ==================== UI Update Functions")]
    script = """
const assert = require('node:assert/strict');
const handlers = {}, socket = {on(name,callback) {handlers[name]=callback;}};
class Element {
    constructor(fragment=false) {this.children=[];this.fragment=fragment;this.textContent='';}
    appendChild(child) {
        if (child.fragment) this.children.push(...child.children.splice(0));
        else this.children.push(child);
    }
    replaceChildren(...children) {this.children=[];children.forEach(child=>this.appendChild(child));}
    removeChild(child) {this.children.splice(this.children.indexOf(child),1);}
    get firstChild() {return this.children[0];}
    get scrollHeight() {return this.children.length;}
    set innerHTML(value) {throw new Error('Console messages must not be parsed as HTML');}
}
const output = new Element();
const document = {
    getElementById(id) {assert.equal(id,'console-output');return output;},
    createElement(tag) {assert.equal(tag,'div');return new Element();},
    createDocumentFragment() {return new Element(true);}
};
function clearDropProgress() {}
const lines = () => output.children.map(child=>child.textContent);
""" + "\n".join(extract_javascript_function(source, name) for name in (
        "addConsoleLine", "addConsoleLineRaw",
    )) + handlers + assertions
    result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("snapshot", [["Current claim", "Current error"], [], ["Same", "Same"]])
def test_reconnect_replaces_old_history_with_complete_snapshot(snapshot):
    run_console_script("""
handlers.initial_state({console:['Old error','Old claim']});
handlers.console_output({message:'Disconnected-era display'});
handlers.initial_state({console:SNAPSHOT});
assert.deepEqual(lines(),SNAPSHOT);
""".replace("SNAPSHOT", json.dumps(snapshot)))


def test_repeated_identical_snapshot_does_not_replay_history():
    run_console_script("""
const snapshot = ['Repeated real message','Repeated real message','Claim confirmed'];
for (let i=0;i<5;i++) handlers.initial_state({console:snapshot});
assert.deepEqual(lines(),snapshot);
assert.equal(output.scrollTop,output.scrollHeight);
""")


@pytest.mark.parametrize("payload", [{}, {"console": None}])
def test_missing_or_null_history_keeps_partial_event_behavior(payload):
    run_console_script("""
handlers.initial_state({console:['Keep me']});
handlers.initial_state(PAYLOAD);
assert.deepEqual(lines(),['Keep me']);
""".replace("PAYLOAD", json.dumps(payload)))


def test_live_messages_append_after_replacement_including_identical_real_messages():
    run_console_script("""
handlers.initial_state({console:['Old']});
handlers.initial_state({console:['New']});
handlers.console_output({message:'New'});
handlers.console_output({message:'Next'});
assert.deepEqual(lines(),['New','New','Next']);
""")


def test_snapshot_and_live_updates_keep_latest_thousand_lines():
    run_console_script("""
const snapshot = Array.from({length:1005},(_,i)=>`Line ${i}`);
handlers.initial_state({console:['Old']});
handlers.initial_state({console:snapshot});
assert.deepEqual(lines(),snapshot.slice(-1000));
handlers.console_output({message:'Latest'});
assert.deepEqual(lines(),[...snapshot.slice(-999),'Latest']);
assert.equal(output.scrollTop,output.scrollHeight+1);
""")


def test_console_snapshot_and_live_messages_remain_literal_text():
    run_console_script("""
const message = '<img src=x onerror="throw new Error(1)"><script>bad()</script>&';
handlers.initial_state({console:[message]});
handlers.initial_state({console:[message]});
handlers.console_output({message});
assert.deepEqual(lines(),[message,message]);
assert.ok(output.children.every(child=>child.children.length===0));
""")
