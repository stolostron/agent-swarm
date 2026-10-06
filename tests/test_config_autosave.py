"""Behavioral regression tests for session configuration autosave."""

import shutil
import subprocess
from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).parents[1] / "swarmer/templates/sessions/detail.html"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="Node.js is needed to execute the browser autosave code")
@pytest.mark.parametrize("scenario", ["trailing-save", "rejected-response"])
def test_mcp_autosave_behavior(scenario: str):
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(0, 'utf8');
const start = html.indexOf('// ── Auto-save configuration fields ──');
const close = html.indexOf('\n})();', start) + '\n})();'.length;
assert.ok(start >= 0 && close > start, 'autosave block is present');
const autosave = html.slice(start, close);

const dirty = { value: '0' };
const changeListeners = {};
const mcpControl = {
  name: 'mcp_server_ids',
  checked: false,
  addEventListener(name, callback) { changeListeners[name] = callback; },
};
const form = {
  action: '/sessions/1/edit',
  querySelector(selector) {
    if (selector === '[name="mcp_settings_changed"]') return dirty;
    return null;
  },
  querySelectorAll(selector) {
    return selector === '[data-mcp-selection-control]' ? [mcpControl] : [];
  },
};
const indicator = { textContent: '', className: '', style: { display: '', color: '', fontSize: '' } };
const requests = [];
const resolveRequests = [];
let repoListRefreshes = 0;
const context = {
  document: {
    getElementById(id) {
      if (id === 'cfg-edit-form') return form;
      if (id === 'cfg-save-indicator') return indicator;
      return null;
    },
    body: { dispatchEvent() { repoListRefreshes++; } },
  },
  window: {},
  FormData: class {
    constructor() {
      this.selection = mcpControl.checked;
      this.dirty = dirty.value;
    }
  },
  Event: class {},
  setTimeout() {},
  fetch(_url, options) {
    requests.push(options.body);
    return new Promise(resolve => resolveRequests.push(resolve));
  },
};
vm.runInNewContext(autosave, context);

async function run() {
  if (process.argv[1] === 'trailing-save') {
    mcpControl.checked = true;
    changeListeners.change();
    const firstSave = context.window._cfgSave();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].selection, true);

    mcpControl.checked = false;
    changeListeners.change();
    context.window._cfgSave();
    assert.equal(requests.length, 1, 'requests stay serialized while the first save is pending');

    resolveRequests[0]({ ok: true, status: 200 });
    await firstSave;
    assert.equal(requests.length, 2, 'a changed selection schedules a trailing request');
    assert.equal(requests[1].selection, false, 'the trailing request captures the latest selection');
    assert.equal(dirty.value, '1', 'the first response leaves newer MCP changes dirty');

    resolveRequests[1]({ ok: true, status: 200 });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(dirty.value, '0', 'the trailing successful save clears the dirty marker');
    assert.equal(indicator.textContent, 'Saved');
  } else {
    mcpControl.checked = true;
    changeListeners.change();
    const failedSave = context.window._cfgSave();
    resolveRequests[0]({ ok: false, status: 500 });
    await failedSave;
    assert.equal(indicator.textContent, 'Save failed');
    assert.equal(dirty.value, '1', 'a rejected HTTP response does not clear the MCP dirty marker');
    assert.equal(repoListRefreshes, 0, 'a rejected HTTP response is not reported as saved');
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        [str(_NODE), "-e", harness, scenario],
        input=_TEMPLATE.read_text(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
