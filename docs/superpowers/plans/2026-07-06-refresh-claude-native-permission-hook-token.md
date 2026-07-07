# Refresh claude-native permission-hook token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop host-spawned claude-native sessions from permanently fail-closing their `UserPromptSubmit`/`PreToolUse`/`PostToolUse` policy hooks once the one-shot bearer baked into `permission_hook.json` at launch expires.

**Architecture:** `permission_hook.json` (written once by `build_hook_settings()` in `claude_native_bridge.py` at session launch) carries a bearer minted via `POST /v1/runners/{id}/token`, which is hard-capped at 1800s (`_MANAGED_RUNNER_TOKEN_TTL_S` in `server/routes/runner_tunnel.py`). The hook subprocess (`claude_native_hook.py evaluate-policy`) replays this static file on every call and has no way to mint a replacement — it lacks `OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN`, which only the parent runner process holds (and must keep holding only there — it's a powerful credential and must not leak into the sandboxed tool-execution environment). The runner process itself, however, already re-mints tokens on demand via `_make_auth_token_factory()` for its own long-lived transcript forwarder (`_RunnerDatabricksAuth`). This plan adds a small periodic task, running alongside that same forwarder inside the runner process, that re-mints a token and rewrites `permission_hook.json` before the baked one expires.

**Tech Stack:** Python 3.12, asyncio, existing `omnigent.claude_native_bridge` / `omnigent.runner.app` modules.

## Global Constraints

- Do not add a new dependency — `asyncio`, `json`, `time`, `pathlib` (already imported in both touched files) cover this.
- Do not propagate `OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN` (or any binding-token-derived long-lived credential) into the hook subprocess's environment or into `permission_hook.json` itself — only short-lived bearers already produced by the existing mint flow may be written to that file.
- Scope is the host-spawned claude-native launch path only (`runner/app.py` around the `_forwarder_task = asyncio.create_task(supervise_forwarder(...))` call under the `claude-forwarder-{session_id}` name). The same one-shot-bake pattern exists for codex-native / kiro-native / opencode-native (see `_runner_auth = _RunnerDatabricksAuth(...)` at `runner/app.py:2235,2420,2653,2792,3163`) — out of scope for this plan; extend by repeating Task 2's wiring at those call sites if/when they're confirmed to hit the same failure.
- Refresh interval must stay comfortably under the 1800s TTL so a slow tick (event loop briefly busy) never crosses the expiry boundary: use 1200s (20 minutes).

---

### Task 1: Add a pure helper to rewrite `permission_hook.json`'s auth headers

**Files:**
- Modify: `omnigent/claude_native_bridge.py` (new function, placed directly after `read_permission_hook_config` at line 1005)
- Test: `tests/test_claude_native_bridge_permission_hook_refresh.py` (new file)

**Interfaces:**
- Produces: `refresh_permission_hook_headers(bridge_dir: Path, *, ap_server_url: str, ap_auth_headers: dict[str, str]) -> None` — later tasks (Task 2) call this on a timer.

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_native_bridge_permission_hook_refresh.py`:

```python
import json
from pathlib import Path

from omnigent.claude_native_bridge import (
    _PERMISSION_HOOK_FILE,
    read_permission_hook_config,
    refresh_permission_hook_headers,
)


def test_refresh_permission_hook_headers_overwrites_existing_file(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hook_path = bridge_dir / _PERMISSION_HOOK_FILE
    hook_path.write_text(
        json.dumps(
            {
                "ap_server_url": "http://127.0.0.1:8000",
                "ap_auth_headers": {"Authorization": "Bearer stale-token"},
                "updated_at": 1000.0,
            }
        ),
        encoding="utf-8",
    )

    refresh_permission_hook_headers(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8000",
        ap_auth_headers={"Authorization": "Bearer fresh-token"},
    )

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_auth_headers"] == {"Authorization": "Bearer fresh-token"}
    assert config["ap_server_url"] == "http://127.0.0.1:8000"
    assert config["updated_at"] > 1000.0


def test_refresh_permission_hook_headers_creates_file_if_missing(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    refresh_permission_hook_headers(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8000",
        ap_auth_headers={"Authorization": "Bearer fresh-token"},
    )

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_auth_headers"] == {"Authorization": "Bearer fresh-token"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/claude/omnigent && python -m pytest tests/test_claude_native_bridge_permission_hook_refresh.py -v`
Expected: FAIL with `ImportError: cannot import name 'refresh_permission_hook_headers'`

- [ ] **Step 3: Write minimal implementation**

In `omnigent/claude_native_bridge.py`, immediately after `read_permission_hook_config` (after line 1005, before `def build_mcp_config`), add:

```python
def refresh_permission_hook_headers(
    bridge_dir: Path,
    *,
    ap_server_url: str,
    ap_auth_headers: dict[str, str],
) -> None:
    """
    Overwrite ``permission_hook.json`` with a freshly minted bearer.

    The claude-native ``evaluate-policy`` / ``permission-request`` command
    hooks are stateless subprocesses that replay this file's static
    headers on every call — they have no credential of their own to mint
    a replacement when the baked bearer expires (see
    ``_MANAGED_RUNNER_TOKEN_TTL_S`` in ``server/routes/runner_tunnel.py``,
    currently 1800s). Call this periodically, from the long-lived runner
    process that DOES hold a mintable credential, before that TTL elapses.

    :param bridge_dir: Bridge directory path.
    :param ap_server_url: Omnigent server base URL, e.g.
        ``"http://127.0.0.1:8000"``.
    :param ap_auth_headers: Freshly minted headers, e.g.
        ``{"Authorization": "Bearer <token>"}``.
    :returns: None.
    """
    _write_json_file(
        bridge_dir / _PERMISSION_HOOK_FILE,
        {
            "ap_server_url": ap_server_url,
            "ap_auth_headers": ap_auth_headers,
            "updated_at": time.time(),
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/claude/omnigent && python -m pytest tests/test_claude_native_bridge_permission_hook_refresh.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /home/claude/omnigent
git add omnigent/claude_native_bridge.py tests/test_claude_native_bridge_permission_hook_refresh.py
git commit -m "feat(claude-native): add helper to refresh permission_hook.json auth headers"
```

---

### Task 2: Periodically refresh the token for host-spawned claude-native sessions

**Files:**
- Modify: `omnigent/runner/app.py:5857-5887` (the `_forwarder_task = asyncio.create_task(supervise_forwarder(...))` block for the host-spawned claude-native launch path)

**Interfaces:**
- Consumes: `refresh_permission_hook_headers(bridge_dir, *, ap_server_url, ap_auth_headers)` from Task 1; `_auth_factory` (already in scope at this point in `runner/app.py`, built at line ~5417 via `_make_auth_token_factory()`); `databricks_request_headers` (already imported at line ~5429); `server_url` and `bridge_dir` (already in scope).
- Produces: nothing consumed elsewhere — this task only has a side effect (rewriting the file on disk).

- [ ] **Step 1: Add the refresh-loop coroutine and wrap the forwarder task**

In `omnigent/runner/app.py`, replace the existing block (currently):

```python
    from omnigent.claude_native_forwarder import supervise_forwarder

    _forwarder_task = asyncio.create_task(
        supervise_forwarder(
            base_url=server_url,
            headers=_runner_headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=resume_external_session_id is not None,
            auth=_runner_auth,
        ),
        name=f"claude-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
```

with:

```python
    from omnigent.claude_native_forwarder import supervise_forwarder

    # The permission-hook bearer baked into permission_hook.json at launch
    # (above, via augment_claude_args) is hard-capped at 1800s
    # (_MANAGED_RUNNER_TOKEN_TTL_S) and the hook subprocess that replays it
    # has no way to mint a replacement. Re-mint and rewrite the file well
    # before that TTL elapses, for as long as this session's forwarder runs,
    # so a long-lived host-spawned session doesn't permanently fail-closed
    # its policy hooks after ~30 minutes.
    _PERMISSION_HOOK_REFRESH_INTERVAL_S = 1200.0

    async def _refresh_permission_hook_forever() -> None:
        from omnigent.claude_native_bridge import refresh_permission_hook_headers

        while True:
            await asyncio.sleep(_PERMISSION_HOOK_REFRESH_INTERVAL_S)
            fresh_token = _auth_factory() if _auth_factory is not None else None
            fresh_headers = databricks_request_headers(server_url, bearer_token=fresh_token)
            await asyncio.to_thread(
                refresh_permission_hook_headers,
                bridge_dir,
                ap_server_url=server_url,
                ap_auth_headers=fresh_headers,
            )

    async def _run_forwarder_and_hook_refresh() -> None:
        await asyncio.gather(
            supervise_forwarder(
                base_url=server_url,
                headers=_runner_headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=resume_external_session_id is not None,
                auth=_runner_auth,
            ),
            _refresh_permission_hook_forever(),
        )

    _forwarder_task = asyncio.create_task(
        _run_forwarder_and_hook_refresh(),
        name=f"claude-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
```

This keeps the existing `_register_auto_forwarder_task` / `_cancel_auto_forwarder_task` registry untouched: cancelling the one registered task (already wired for terminal re-create and session teardown) cancels both the forwarder and the refresh loop together, since `asyncio.gather` propagates cancellation to both children.

- [ ] **Step 2: Verify the module still imports cleanly**

Run: `cd /home/claude/omnigent && python -c "import omnigent.runner.app"`
Expected: no output, exit code 0 (syntax/import sanity check — this function body isn't unit-testable without a live server + tmux + Claude binary; see manual verification below).

- [ ] **Step 3: Manual end-to-end verification**

This path only runs inside a real host-spawned claude-native session (tmux + runner + live Omnigent server), so verify manually rather than with a unit test:

1. Start a claude-native session against a local Omnigent server (`omnigent claude` or via the web UI "New Chat").
2. Find its bridge dir: `ls -t /tmp/omnigent-*/claude-native/ | head -1`.
3. Note `permission_hook.json`'s `updated_at`.
4. Wait 20 minutes (`_PERMISSION_HOOK_REFRESH_INTERVAL_S`).
5. Re-check `permission_hook.json`'s `updated_at` — it should have advanced by ~1200s, and the embedded JWT's `exp` claim (base64-decode the second dot-segment of the `Authorization: Bearer <jwt>` value) should be ~30 minutes in the future from the new `updated_at`, not from session launch.
6. Type a new prompt in that session after the 30-minute mark from launch (when the original bake would have expired). Confirm no `UserPromptSubmit operation blocked by hook: Omnigent policy evaluation unavailable` message appears.

Expected: `permission_hook.json` refreshes every ~20 minutes for the life of the session, and prompts submitted well past the original 30-minute window succeed normally.

- [ ] **Step 4: Commit**

```bash
cd /home/claude/omnigent
git add omnigent/runner/app.py
git commit -m "fix(runner): periodically refresh claude-native permission_hook.json token"
```

---

## Self-Review

**Spec coverage:** The reported bug (host-spawned claude-native session's `permission_hook.json` bearer expires at 1800s with nothing to refresh it, permanently fail-closing `UserPromptSubmit`/`PreToolUse`/`PostToolUse` hooks) is covered end-to-end: Task 1 adds the rewrite primitive, Task 2 schedules it on a safe interval and wires its lifecycle into the existing forwarder-task registry so cleanup (cancel-on-recreate, cancel-on-teardown) needs no new code.

**Placeholder scan:** No TBD/TODO markers; every step has literal code.

**Type consistency:** `refresh_permission_hook_headers(bridge_dir: Path, *, ap_server_url: str, ap_auth_headers: dict[str, str]) -> None` is defined once in Task 1 and called with matching keyword names in Task 2.
