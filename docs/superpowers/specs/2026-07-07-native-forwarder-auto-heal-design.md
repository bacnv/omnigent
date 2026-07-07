# Native Forwarder Auto-Heal Design

## Goal

Keep claude-native web UI mirroring healthy when Claude and tmux are still alive but the runner-side transcript forwarder has died or gone stale.

The first phase is deliberately conservative: recover only the forwarder and hook-refresh coroutine. Do not kill Claude, tmux, the harness, the runner, or the host.

## Non-goals

- Detecting or killing a truly hung Claude process.
- Recreating tmux panes or terminals.
- Restarting the host daemon or runner process.
- Adding UI controls in the first phase.
- Persisting health history beyond logs and in-memory counters.

## Phase 1: Conservative auto-heal

### Scope

Apply to claude-native sessions launched by the runner auto-create path.

Extend the existing `supervise_forwarder` loop rather than adding a second runner-level restart loop. The runner should keep launching one registered combined task for forwarder + hook refresh; transcript crash/stale handling lives inside, or immediately next to, the current supervisor so crash/return backoff stays single-owned. The hook-refresh loop must handle its own retryable exceptions so it cannot tear down the combined task while the transcript forwarder is healthy.

### Health signals

Use the smallest signals already available around the forwarder:

1. **Forwarder or combined task exited or crashed**
   - Forwarder loop crashes are already handled by `supervise_forwarder`; keep using that path and its bounded backoff.
   - Hook-refresh exceptions must be caught and retried inside `_refresh_permission_hook_forever()` so `asyncio.gather()` does not cancel the healthy transcript forwarder.
   - If the registered combined task exits anyway, restart it only when the active predicate below still says the native session is live.
   - Do not add a runner watchdog that competes with the inner transcript supervisor.

2. **Poll loop stopped making progress**
   - Track the last successful forwarder poll, not just POST success.
   - Track whether transcript/delta/hook inputs are advancing and whether the loop is actively retrying delivery.
   - Restart only when inputs keep advancing but the forwarder has stopped polling or updating its own health marker.
   - Do not restart merely because delivery is failing: server/network outages, retry backoff, delta-ordering holds, non-postable transcript changes, and best-effort status failures should keep the existing retry path.

3. **Bridge metadata drift**
   - Only correct launch-time `BRIDGE_ID_LABEL_KEY` drift before any rotation or transfer is observed.
   - Legal drift: `active_session_id` changes from `/clear` or `/fork`; a shared bridge whose live terminal is arriving via `_claude_native_terminal_arrives_via_transfer()`; and a cleared-session bridge id such as `{session_id}-cleared`.
   - Never reassert the original session id after rotation, and never overwrite `active_session_id` from auto-heal.
   - Do not wipe bridge state or transcript cursors.

### Active predicate

Auto-heal may restart a registered combined task only when all of these are true at decision time:

- `_AUTO_FORWARDER_TASKS.get(session_id) is task` for the task being recovered, so an intentional cancellation, done-callback eviction, or successor task blocks resurrection;
- the terminal registry still has a live `claude:main` terminal for the current active session;
- the bridge id still matches the session binding, or the mismatch is one of the legal rotation/transfer states above.

If any part is false, auto-heal logs and stops; terminal recreate, intentional teardown, and cancellation must not resurrect a forwarder.

### Recovery action

Restart only the registered combined forwarder plus hook-refresh task using the same launch context:

- `base_url`
- `headers`
- refresh-capable `auth`
- `session_id`
- `bridge_dir`
- `agent_name`
- original launch `start_at_end` value, unless a durable forward cursor already exists

Reuse the existing forward state and transcript cursor so a restart does not replay old transcript items. If there is no durable cursor yet, preserve the original launch `start_at_end` value; forcing `start_at_end=True` on a fresh session can skip transcript lines that were written but not yet forwarded.

### Backoff

Reuse the existing `supervise_forwarder` bounded backoff for crash/return restarts.

For stale-health cancellation, feed the restart through the same supervisor/backoff path instead of layering a second independent backoff in the runner.

- Crash/return restarts keep the existing `_SUPERVISOR_INITIAL_BACKOFF_S` delay.
- Stale detection may cancel the stale child immediately after the threshold is confirmed, but the replacement start still uses the supervisor/backoff policy.
- Later stale cancellations back off to avoid a tight cancellation loop.
- Log restart reason, health snapshot, attempt count, and next delay.

### Safety rules

Phase 1 must not:

- kill Claude;
- kill tmux;
- recreate the terminal;
- reset or delete the bridge directory;
- reset transcript forwarding state;
- restart the host, runner, or harness process.

The only destructive operation allowed is cancelling the stale forwarder child or stale registered combined task before starting a replacement, and only when the active predicate still holds.

## Phase 2 backlog

These are explicit limitations of Phase 1 and should be handled separately.

### Claude process hangs

Phase 1 does not detect or recover a truly hung Claude process.

Phase 2 can add terminal liveness checks and a safe user-approved flow to resume the session in a new Claude process or tmux pane.

### Orphaned tmux or process recovery

Phase 1 assumes tmux and Claude are still reachable through the existing bridge context.

Phase 2 can scan orphaned tmux panes by bridge/session label and reattach a new runner-side forwarder to them.

### MCP bridge server death

Phase 1 restarts the transcript forwarder, but it does not restart the MCP server process launched by Claude settings.

Phase 2 can add bridge health pings, MCP-side status, and recovery guidance when the MCP server is dead.

### UI status and controls

Phase 1 is log-only.

Phase 2 can expose native session health in the session resource/status payload and add UI controls such as “Reconnect forwarder”.

### Persistent health history

Phase 1 keeps counters in memory and logs events.

Phase 2 can persist health events for postmortem debugging after runner or host restarts.

### Escalating recovery

Phase 1 avoids destructive recovery.

Phase 2 can add opt-in escalation after repeated forwarder restart failures, such as recreating the native terminal or restarting Claude.

## Testing

Phase 1 needs focused tests for supervisor and stale-health behavior:

1. Existing `supervise_forwarder` still restarts when the forwarder raises or returns, preserving the tested initial backoff.
2. Hook-refresh exceptions are retried locally and do not tear down the combined registered task.
3. If the combined task exits unexpectedly while the active predicate holds, recovery creates exactly one replacement registered task.
4. If the active predicate fails after intentional cancel, terminal recreate, or successor registration, recovery does not resurrect the task.
5. Advancing inputs plus stale forwarder poll marker cancels and restarts through the same supervisor path.
6. Delivery failures with active retry state do not trigger stale cancellation.
7. Restart preserves the original `start_at_end` until a durable cursor exists, avoiding both replay and skipped transcript lines.
8. `/clear`, `/fork`, transfer-in, and `{session_id}-cleared` states are not treated as metadata drift.
9. Repeated stale cancellations use backoff instead of tight restart loops.
10. There is exactly one live registered forwarder task for the session after recovery.
11. Recovery never calls Claude/tmux/host/runner kill paths.

## Implementation notes

Prefer the smallest diff:

- keep stale-health logic inside or adjacent to `supervise_forwarder`, not as an independent runner loop;
- expose only the minimum monotonic heartbeat/health state from the forwarder;
- avoid a general native-session manager until Phase 2 evidence requires it.
