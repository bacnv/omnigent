"""End-to-end tests for the generated pi-native bridge extension."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_delivery_cap_drops_followup_without_failed_session_status(
    tmp_path: Path,
) -> None:
    """The extension must not terminal-fail a session when follow-up delivery caps.

    This runs the real JavaScript extension under Node with a real inbox payload
    and mocked Pi/fetch boundaries. Five consecutive ``sendUserMessage`` throws
    should consume the inbox file and emit an informational conversation item,
    never ``external_session_status`` with ``status: "failed"``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-msg.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "msg-1", type: "user_message", content: "follow up" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const sendAttempts = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage(content, options) {
    sendAttempts.push({ content, options });
    throw new Error("Pi is not ready");
  },
};

require(extensionPath)(pi);

(async () => {
  assert.equal(typeof handlers.session_start, "function");
  await handlers.session_start({}, {
    sessionManager: { getSessionId: () => "native-session-1" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
  });
  assert.equal(typeof pollInbox, "function");

  for (let attempt = 0; attempt < 5; attempt += 1) {
    pollInbox();
  }
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    sendAttempts,
    Array.from({ length: 5 }, () => ({
      content: "follow up",
      options: { deliverAs: "followUp" },
    })),
  );
  assert.equal(fs.existsSync(payloadPath), false);
  assert.equal(
    postedEvents.some(
      (event) =>
        event.type === "external_session_status" &&
        event.data &&
        event.data.status === "failed",
    ),
    false,
    JSON.stringify(postedEvents),
  );

  const dropNote = postedEvents.find(
    (event) =>
      event.type === "external_conversation_item" &&
      event.data &&
      event.data.item_type === "error" &&
      event.data.item_data &&
      event.data.item_data.code === "pi_followup_delivery_dropped",
  );
  assert.ok(dropNote, JSON.stringify(postedEvents));
  assert.equal(dropNote.data.item_data.source, "execution");
  assert.match(dropNote.data.response_id, /^pi-deliver-dropped-/);
  // The note must be actionable: include the dropped message id and a preview
  // of its content so an operator can identify what was lost.
  assert.match(dropNote.data.item_data.message, /msg-1/);
  assert.match(dropNote.data.item_data.message, /follow up/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ── TOOL_CALL policy evaluation (DENY / ALLOW / ASK elicitation) ──────────
#
# These exercise the real evalNativePolicyHttp park/resolve loop in the
# generated extension by driving its tool_call handler under Node with a
# scripted fetch and assert-rich JS body. Each case supplies a queue of
# responses; the JS harness drives one tool_call and reports the verdict.

# Shared JS preamble: loads the extension, wires a scripted fetch + fake
# timers (so the long park / backoff budgets collapse to instant in test),
# and exposes runToolCall() which fires the tool_call handler and returns the
# verdict the extension would hand back to Pi.
_POLICY_HARNESS_PREAMBLE = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const configPath = path.join(tmpDir, "config.json");

fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    authHeaders: { authorization: "Bearer test" },
  }),
);
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

// Captured evaluate-request bodies (parsed) in call order.
const evalBodies = [];
// Queue of responders. Each entry is a function (parsedBody) -> response-like
// object, OR the string "THROW" to simulate a transport error, OR
// "THROW_ABORT" to simulate our own AbortController firing (DOMException-ish).
let responders = [];
let evalCallCount = 0;

function makeJsonResponse(obj, status) {
  return {
    ok: status === undefined || (status >= 200 && status < 300),
    status: status === undefined ? 200 : status,
    json: async () => obj,
  };
}

global.fetch = async (url, request) => {
  const body = JSON.parse(request.body);
  // Only the evaluate endpoint is scripted; postEvent calls (events endpoint)
  // just succeed silently so they never interfere with the verdict assertions.
  if (typeof url === "string" && url.includes("/policies/evaluate")) {
    evalBodies.push(body);
    const idx = evalCallCount;
    evalCallCount += 1;
    const responder = responders[idx];
    if (responder === "THROW") {
      throw new Error("ECONNREFUSED simulated transport error");
    }
    if (responder === "THROW_ABORT") {
      // Simulate our own AbortController firing mid-park: flip the request's
      // signal to aborted (this is the SAME signal object the extension's
      // controller exposes), then throw the AbortError fetch would raise. The
      // extension's catch checks controller.signal.aborted to distinguish a
      // re-park from a transient transport error.
      if (request && request.signal && typeof request.signal._abort === "function") {
        request.signal._abort();
      }
      const err = new Error("The operation was aborted");
      err.name = "AbortError";
      throw err;
    }
    if (typeof responder === "function") return responder(body);
    // Default: allow.
    return makeJsonResponse({ result: "POLICY_ACTION_ALLOW" });
  }
  return { ok: true, status: 200, json: async () => ({}) };
};

// Fake clock + timers. A short delay (sleep/backoff) advances a virtual clock
// by its duration and fires on the next microtask, so the extension's
// wall-clock budgets (transient retry, park ceiling) elapse deterministically
// and instantly — no real 30s wait. The long park abort timer (>= 100s) is
// never fired so a scripted fetch always resolves first; but scheduling it
// still advances nothing (it is cleared in the finally).
let fakeNow = 1_000_000;
const realDateNow = Date.now.bind(Date);
Date.now = () => fakeNow;
global.setTimeout = (fn, ms) => {
  if (typeof ms === "number" && ms >= 100000) {
    return { fakeBig: true };
  }
  if (typeof ms === "number" && ms > 0) fakeNow += ms;
  Promise.resolve().then(fn);
  return { fakeSmall: true };
};
global.clearTimeout = () => {};
// Keep the inbox poller dormant.
global.setInterval = () => ({ fakeInterval: true });

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle() { return false; },
};

async function runToolCall() {
  assert.equal(typeof handlers.tool_call, "function");
  return handlers.tool_call(
    { toolCallId: "call-1", toolName: "Bash", input: { command: "rm -rf /tmp/x" } },
    ctx,
  );
}
"""


def _run_policy_node_script(extension_path: Path, tmp_path: Path, body: str) -> None:
    """Run the extension's tool_call policy path under Node with a scripted fetch.

    :param extension_path: Path to the generated extension JS.
    :param tmp_path: Per-test scratch dir (config is written here).
    :param body: JS test body appended after the shared harness preamble; it
        sets ``responders`` and runs assertions inside an async IIFE.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension policy test")

    script = _POLICY_HARNESS_PREAMBLE + "\n" + body
    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _extension_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )


def test_policy_allow_proceeds(tmp_path: Path) -> None:
    """An ALLOW verdict lets the Pi tool call proceed (no block returned)."""
    body = r"""
(async () => {
  responders = [(_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" })];
  const verdict = await runToolCall();
  // tool_call returns undefined (or a non-blocking value) on ALLOW.
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 1);
  // The evaluate body must carry a valid re-attach id and a PHASE_TOOL_CALL.
  assert.match(evalBodies[0]._omnigent_elicitation_id, /^elicit_evaluate_[0-9a-f]{32}$/);
  assert.equal(evalBodies[0].event.type, "PHASE_TOOL_CALL");
  assert.equal(evalBodies[0].event.data.name, "Bash");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_deny_blocks(tmp_path: Path) -> None:
    """A DENY verdict blocks the Pi tool call and surfaces the policy reason."""
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "no rm -rf" }),
  ];
  const verdict = await runToolCall();
  assert.deepEqual(verdict, { block: true, reason: "no rm -rf" });
  assert.equal(evalBodies.length, 1);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_ask_parks_then_resolves_allow(tmp_path: Path) -> None:
    """A raw ASK re-evaluates (re-attaching) until it resolves to ALLOW.

    The first evaluate returns ASK (gate did not park server-side); the loop
    re-POSTs the SAME elicitation id and the second returns ALLOW, so the tool
    call proceeds. Mirrors the server collapsing ASK to a hard verdict.
    """
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "approve?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" }),
  ];
  const verdict = await runToolCall();
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 2, "expected park-then-resolve = 2 evaluates");
  // Both POSTs must reuse the SAME elicitation id so the server re-attaches
  // rather than opening a second approval card.
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_ask_parks_then_resolves_deny(tmp_path: Path) -> None:
    """A raw ASK that resolves to DENY blocks the Pi tool call with the reason."""
    body = r"""
(async () => {
  responders = [
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_ASK", reason: "approve?" }),
    (_b) => makeJsonResponse({ result: "POLICY_ACTION_DENY", reason: "declined" }),
  ];
  const verdict = await runToolCall();
  assert.deepEqual(verdict, { block: true, reason: "declined" });
  assert.equal(evalBodies.length, 2);
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_park_abort_reattaches_same_id(tmp_path: Path) -> None:
    """An aborted park (our own headers-timeout guard) re-attaches, not re-mints.

    Simulates undici severing a long park: the first attempt throws an
    AbortError (signal.aborted), the loop must re-POST the SAME elicitation id
    immediately (no backoff), and the resolved ALLOW lets the tool proceed.
    """
    body = r"""
(async () => {
  // First call: pretend our AbortController fired (the fake controller below
  // reports aborted=true). Second call: resolved ALLOW.
  responders = ["THROW_ABORT", (_b) => makeJsonResponse({ result: "POLICY_ACTION_ALLOW" })];
  // Fake AbortController whose signal exposes a private _abort() so the fetch
  // mock can flip aborted=true (mimicking our 240s headers-timeout guard
  // firing). The extension's catch reads controller.signal.aborted to choose
  // the re-attach (no-backoff) branch over the transient-error branch.
  global.AbortController = class {
    constructor() {
      let aborted = false;
      const signal = {};
      Object.defineProperty(signal, "aborted", { get() { return aborted; } });
      signal._abort = () => { aborted = true; };
      this.signal = signal;
    }
    abort() { this.signal._abort(); }
  };
  const verdict = await runToolCall();
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  assert.equal(evalBodies.length, 2, "expected re-attach after abort = 2 evaluates");
  assert.equal(
    evalBodies[0]._omnigent_elicitation_id,
    evalBodies[1]._omnigent_elicitation_id,
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)


def test_policy_transport_error_fails_open(tmp_path: Path) -> None:
    """A persistent transport error fails OPEN so a server outage never wedges Pi.

    Every evaluate POST throws a non-abort transport error; after the transient
    retry budget elapses the extension returns null (fail open) and the tool
    call proceeds without a block.
    """
    body = r"""
(async () => {
  // Always throw a transport error; with fake timers collapsing the backoff
  // the transient budget elapses quickly and the loop fails open.
  responders = new Array(64).fill("THROW");
  const verdict = await runToolCall();
  // Fail open → undefined / non-blocking.
  assert.ok(!verdict || verdict.block !== true, JSON.stringify(verdict));
  // It must have actually retried a few times before giving up (not one-shot).
  assert.ok(evalBodies.length >= 2, "expected retries, got " + evalBodies.length);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""
    _run_policy_node_script(_extension_path(), tmp_path, body)
