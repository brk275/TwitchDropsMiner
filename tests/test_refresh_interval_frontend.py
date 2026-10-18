"""Refresh controls preserve valid values and report failed saves safely."""

import json
from pathlib import Path

import pytest

from tests.javascript_helpers import NODE
from tests.test_account_link_frontend import SETUP
from tests.test_telegram_frontend import run_javascript


pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for frontend tests")
ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ["onRefreshIntervalChange", "updateSettingsUI", "toggleMiningHoursRange"]
UI_SETUP = SETUP + r"""
const refresh = document.getElementById('minimum-refresh-interval');
const refreshError = document.getElementById('minimum-refresh-error');
state.settings.minimum_refresh_interval_minutes = 120;
state.translations.gui.settings.minimum_refresh_save_error = '<img src=x> Save failed';
let validityReports = 0;
Object.defineProperty(refresh, 'valueAsNumber', {get() { return Number(this.value); }});
refresh.reportValidity = function() {
    validityReports++;
    const value = this.valueAsNumber;
    return String(this.value).trim() !== '' && Number.isInteger(value) && value >= 1 && value <= 1440;
};
"""


def test_unrelated_autosave_omits_interval_even_with_stale_input():
    run_javascript(["saveSettings"], SETUP + r"""
function getInventoryFilters() { return {}; }
function parseDropNameBlacklist() { return []; }
const refresh = document.getElementById('minimum-refresh-interval');
refresh.value = '30';
state.settings.minimum_refresh_interval_minutes = 1;
let sent = false;
global.fetch = async (url, options) => {
    const update = JSON.parse(options.body);
    assert.equal(Object.hasOwn(update, 'minimum_refresh_interval_minutes'), false);
    sent = true;
    return {ok: true};
};
saveSettings().then(() => {
    assert.equal(sent, true);
}).catch(error => { console.error(error); process.exitCode = 1; });
""")


@pytest.mark.parametrize("value", (1, 30, 1440))
def test_valid_save_disables_control_and_confirms_exact_value(value):
    run_javascript(FUNCTIONS, UI_SETUP + f"""
refresh.value = '{value}';
let finish, calls = 0;
global.fetch = async (url, options) => {{
    calls++;
    assert.equal(refresh.disabled, true);
    assert.equal(url, '/api/settings');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {{minimum_refresh_interval_minutes: {value}}});
    return new Promise(resolve => {{finish = resolve;}});
}};
(async () => {{
    const pending = onRefreshIntervalChange();
    assert.equal(refresh.disabled, true);
    await onRefreshIntervalChange();
    assert.equal(calls, 1, 'duplicate pending saves must be ignored');
    finish({{ok: true, json: async () => ({{success: true, settings: {{minimum_refresh_interval_minutes: {value}}}}})}});
    await pending;
    assert.equal(refresh.disabled, false);
    assert.equal(Number(refresh.value), {value});
    assert.equal(state.settings.minimum_refresh_interval_minutes, {value});
    assert.equal(refreshError.hidden, true);
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
""")


@pytest.mark.parametrize("value", ("", "0", "-1", "1.5", "1441", "NaN", "Infinity"))
def test_invalid_native_input_is_not_truncated_or_sent(value):
    run_javascript(FUNCTIONS, UI_SETUP + f"""
refresh.value = {json.dumps(value)};
global.fetch = async () => {{throw new Error('Invalid input must not be sent');}};
onRefreshIntervalChange().then(() => {{
    assert.equal(validityReports, 1);
    assert.equal(refresh.disabled, undefined);
    assert.equal(state.settings.minimum_refresh_interval_minutes, 120);
    assert.equal(refresh.value, {json.dumps(value)});
}}).catch(error => {{console.error(error); process.exitCode = 1;}});
""")


@pytest.mark.parametrize("failure", ("network", "http", "application", "mismatch", "old_server", "json", "null"))
def test_failed_save_restores_confirmed_value_and_shows_literal_error(failure):
    run_javascript(FUNCTIONS, UI_SETUP + f"""
refresh.value = '1';
global.fetch = async () => {{
    if ('{failure}' === 'network') throw new Error('synthetic network error');
    return {{ok: '{failure}' !== 'http', json: async () => {{
        if ('{failure}' === 'json') throw new Error('synthetic JSON error');
        if ('{failure}' === 'null') return null;
        return {{success: '{failure}' !== 'application', settings: '{failure}' === 'old_server' ? {{}} : {{
            minimum_refresh_interval_minutes: '{failure}' === 'mismatch' ? 2 : 1
        }}}};
    }}}};
}};
onRefreshIntervalChange().then(() => {{
    assert.equal(Number(refresh.value), 120);
    assert.equal(state.settings.minimum_refresh_interval_minutes, 120);
    assert.equal(refresh.disabled, false);
    assert.equal(refreshError.hidden, false);
    assert.equal(refreshError.textContent, '<img src=x> Save failed');
}}).catch(error => {{console.error(error); process.exitCode = 1;}});
""")


@pytest.mark.parametrize("newer", (
    {"minimum_refresh_interval_minutes": 1, "dark_mode": True},
    {"minimum_refresh_interval_minutes": 60, "dark_mode": False},
))
def test_late_success_preserves_newer_confirmed_settings(newer):
    run_javascript(FUNCTIONS, UI_SETUP + f"""
updateSettingsUI({{minimum_refresh_interval_minutes: 120, dark_mode: false}});
refresh.value = '1';
let finish;
global.fetch = async () => new Promise(resolve => {{finish = resolve;}});
(async () => {{
    const pending = onRefreshIntervalChange();
    const newer = {json.dumps(newer)};
    updateSettingsUI(newer);
    finish({{ok: true, json: async () => ({{success: true, settings: {{
        minimum_refresh_interval_minutes: 1, dark_mode: false
    }}}})}});
    await pending;
    assert.equal(state.settings, newer);
    assert.equal(Number(refresh.value), newer.minimum_refresh_interval_minutes);
    assert.equal(document.getElementById('dark-mode').checked, newer.dark_mode);
    assert.equal(refresh.disabled, false);
    assert.equal(refreshError.hidden, true);
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
""")


def test_interval_ack_does_not_reset_unconfirmed_other_controls():
    run_javascript(FUNCTIONS, UI_SETUP + r"""
updateSettingsUI({minimum_refresh_interval_minutes: 120, dark_mode: false});
refresh.value = '1';
let finish;
global.fetch = async () => new Promise(resolve => {finish = resolve;});
(async () => {
    const pending = onRefreshIntervalChange();
    document.getElementById('dark-mode').checked = true;
    finish({ok: true, json: async () => ({success: true, settings: {
        minimum_refresh_interval_minutes: 1, dark_mode: false
    }})});
    await pending;
    assert.equal(state.settings.minimum_refresh_interval_minutes, 1);
    assert.equal(state.settings.dark_mode, false, 'the unsaved edit is not a server confirmation');
    assert.equal(document.getElementById('dark-mode').checked, true);
    assert.equal(Number(refresh.value), 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
""")


def test_visible_save_error_is_translated_as_text_in_every_locale():
    translations = [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted((ROOT / "lang").glob("*.json"))]
    run_javascript(["applyTranslations"], "const translations = " + json.dumps(translations) + r""";
const assert = require('node:assert/strict');
const error = {hidden: false, textContent: ''};
Object.defineProperty(error, 'innerHTML', {set() { throw new Error('No HTML injection'); }});
const document = {
    getElementById(id) {
        if (id === 'settings-tab') return {querySelector: () => null};
        return id === 'minimum-refresh-error' ? error : null;
    },
    querySelector: () => null,
};
const state = {};
function translateHistory() {}
function renderGamesToWatch() {}
for (const translation of [...translations, {gui: {settings: {minimum_refresh_save_error: '<img src=x>'}}}]) {
    const text = translation.gui.settings.minimum_refresh_save_error;
    assert.equal(typeof text, 'string');
    assert.ok(text.length);
    applyTranslations(translation);
    assert.equal(error.textContent, text);
}
""")


def test_interval_control_has_native_constraints_accessible_error_and_dedicated_handler():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "web/static/app.js").read_text(encoding="utf-8")
    assert 'id="minimum-refresh-interval" min="1" max="1440" step="1" required' in html
    assert 'aria-describedby="minimum-refresh-error"' in html
    assert 'id="minimum-refresh-error" class="help-text" role="alert" hidden' in html
    assert "getElementById('minimum-refresh-interval').addEventListener('change', onRefreshIntervalChange)" in script
