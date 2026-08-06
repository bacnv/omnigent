"""Tests for the privileged Claude permission-hook auth refresh tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner.native.orchestration import (
    _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS,
    _refresh_claude_permission_hook_auth,
    _register_claude_permission_refresh_task,
    teardown_all_claude_native_permission_refreshes,
    teardown_claude_native_permission_refresh,
)

_BRIDGE_DIR = Path("/tmp/bridge-conv_perm")


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Snap the module-global refresh registry so a test can't leak into the next."""
    saved = dict(_AUTO_CLAUDE_PERMISSION_REFRESH_TASKS)
    _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS.clear()
    yield
    _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS.clear()
    _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS.update(saved)


class _SleepGate:
    """``sleep`` fake that blocks each call until ``release`` is set."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.calls = 0

    async def __call__(self, _seconds: float) -> None:
        self.calls += 1
        await self.event.wait()


@pytest.mark.asyncio
async def test_refresh_loop_mints_only_after_sleep_then_restamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The refresh loop sleeps first, then mints + restamps the hook bearer.

    Cadence is event-controlled: the privileged parent must NOT mint a
    token before its interval has elapsed (minting is the expensive /
    rate-limited part). Injecting a ``sleep`` that parks on a gate proves
    no mint happens until the gate opens, and exactly one restamp follows.
    """
    mints: list[str] = []

    def _factory() -> str:
        return f"tok{len(mints)}"

    rewritten: list[tuple[Path, str]] = []

    def _refresh(bridge_dir: Path, authorization: str) -> bool:
        rewritten.append((bridge_dir, authorization))
        return True

    # The loop imports refresh_permission_hook_auth fresh each iteration.
    monkeypatch.setattr("omnigent.claude_native_bridge.refresh_permission_hook_auth", _refresh)

    gate = _SleepGate()
    task = asyncio.create_task(
        _refresh_claude_permission_hook_auth(_BRIDGE_DIR, _factory, sleep=gate)
    )

    try:
        # Parked in the first sleep: no mint, no restamp yet.
        await asyncio.sleep(0)
        assert gate.calls == 1
        assert rewritten == []

        gate.event.set()
        # The mint runs via asyncio.to_thread, so poll deterministically for
        # the restamp instead of assuming a fixed number of loop ticks.
        for _ in range(100):
            await asyncio.sleep(0)
            if rewritten:
                break
        assert rewritten == [(_BRIDGE_DIR, "Bearer tok0")], rewritten

        # After the restamp the loop re-parks in sleep #2.
        for _ in range(100):
            await asyncio.sleep(0)
            if gate.calls == 2:
                break
        assert gate.calls == 2, "loop must re-park in sleep after a restamp"
    finally:
        task.cancel()
        await asyncio.wait({task})


@pytest.mark.asyncio
async def test_refresh_loop_retries_after_factory_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minting failure is best-effort: the loop logs and retries next tick."""
    calls: list[int] = []
    rewritten: list[str] = []

    def _factory() -> str:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("token mint unavailable")
        return "tok-recovered"

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_auth",
        lambda _dir, auth: rewritten.append(auth) or True,
    )

    state = {"sleeps": 0}

    async def _sleep(_s: float) -> None:
        state["sleeps"] += 1
        # Let the recovered mint's to_thread write land before the loop parks
        # again, then end the loop on its third sleep.
        if state["sleeps"] == 3:
            raise asyncio.CancelledError()

    task = asyncio.create_task(
        _refresh_claude_permission_hook_auth(_BRIDGE_DIR, _factory, sleep=_sleep)
    )
    await asyncio.wait({task})

    # First tick raised — no write. Second tick recovered and wrote once.
    assert calls[-1] == 1, "factory must have been retried after the first failure"
    assert rewritten == ["Bearer tok-recovered"], rewritten


@pytest.mark.asyncio
async def test_refresh_loop_skips_write_when_factory_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None`` token means the parent declined to mint → skip the write."""
    rewritten: list[str] = []

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_auth",
        lambda _dir, auth: rewritten.append(auth) or True,
    )

    async def _sleep(_s: float) -> None:
        raise asyncio.CancelledError()

    task = asyncio.create_task(
        _refresh_claude_permission_hook_auth(_BRIDGE_DIR, lambda: None, sleep=_sleep)
    )
    await asyncio.wait({task})
    assert rewritten == [], "None token must not trigger a restamp"


@pytest.mark.asyncio
async def test_refresh_task_survives_independent_forwarder_ending() -> None:
    """
    The refresh task is registry-independent from a session's forwarder.

    Cancelling / completing the transcript forwarder must NOT take down the
    refresh task (they share only a session id, not a lifecycle). A
    parked forwarder ending on its own leaves the refresh task live.
    """
    refresh = _ForwarderRun()

    async def _parked_refresh() -> None:
        refresh.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh.cancelled = True
            raise

    forwarder = _ForwarderRun()

    async def _parked_forwarder() -> None:
        forwarder.task = asyncio.current_task()
        # The forwarder ends on its own (e.g. the child exited cleanly) and
        # so is NOT registered as cancelled.

    try:
        refresh_task = asyncio.create_task(_parked_refresh())
        forwarder_task = asyncio.create_task(_parked_forwarder())
        await asyncio.sleep(0)
        # The forwarder runs to completion independently...
        await asyncio.wait({forwarder_task})
        assert forwarder_task.done()
        # ...while the refresh task is still parked and alive.
        assert not refresh_task.done()
        assert refresh.task is not None
    finally:
        refresh_task.cancel()
        await asyncio.wait({refresh_task})
        assert refresh.cancelled is True


@pytest.mark.asyncio
async def test_register_cancels_incumbent_and_evicts_on_done() -> None:
    """
    Re-registering a session cancels the prior task; a finished task evicts.

    Two registrations for the same session must not keep two refresh loops
    alive (they'd race to restamp and double-mint). And once a task
    finishes (cancellation propagates), its slot is evicted so a re-create
    sees an empty slot.
    """
    session_id = "f98115a89870f7e364064c9d06c52ee7"
    first = _ForwarderRun()

    async def _parked() -> None:
        first.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first.cancelled = True
            raise

    try:
        task1 = asyncio.create_task(_parked())
        _register_claude_permission_refresh_task(session_id, task1)
        await asyncio.sleep(0)
        assert _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS[session_id] is task1

        second = _ForwarderRun()

        async def _parked2() -> None:
            second.task = asyncio.current_task()
            await asyncio.Event().wait()

        task2 = asyncio.create_task(_parked2())
        _register_claude_permission_refresh_task(session_id, task2)
        await asyncio.sleep(0)

        # The incumbent was cancelled by the re-registration.
        assert first.cancelled is True
        assert _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS[session_id] is task2

        # Eviction: a finished task drops its own slot (so a later re-create
        # doesn't see a stale handle). task1 evicts itself once awaited.
        await asyncio.wait({task1})
        assert session_id not in _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS or (
            _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS[session_id] is task2
        )
    finally:
        for t in (_AUTO_CLAUDE_PERMISSION_REFRESH_TASKS.pop(session_id, None),):
            if t is not None and not t.done():
                t.cancel()
                await asyncio.wait({t})
        await _drain([first, second])


@pytest.mark.asyncio
async def test_teardown_cancels_and_awaits_registered_task() -> None:
    """``teardown_claude_native_permission_refresh`` awaits the cancellation."""
    session_id = "c0d3f00d0000000000000000deadbeef"
    run = _ForwarderRun()

    async def _parked() -> None:
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task = asyncio.create_task(_parked())
        _register_claude_permission_refresh_task(session_id, task)
        await asyncio.sleep(0)
        assert not task.done()

        await teardown_claude_native_permission_refresh(session_id)

        assert task.cancelled(), "teardown must await finished-cancellation"
        assert run.cancelled is True
        assert session_id not in _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS
        # Idempotent: a second teardown with no registered task is a no-op.
        await teardown_claude_native_permission_refresh(session_id)
        # A teardown of an already-done task is also a no-op (slot stays empty).
        await teardown_claude_native_permission_refresh(session_id)
    finally:
        await _drain([run])


@pytest.mark.asyncio
async def test_teardown_all_cancels_every_registered_task() -> None:
    """``teardown_all`` cancels and awaits every session's refresh task."""
    sessions = ["sess_a", "sess_b", "sess_c"]
    runs = {sid: _ForwarderRun() for sid in sessions}

    async def _make_parked(run: _ForwarderRun) -> Any:
        async def _parked() -> None:
            run.task = asyncio.current_task()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                run.cancelled = True
                raise

        return _parked

    tasks: dict[str, asyncio.Task[None]] = {}
    try:
        for sid in sessions:
            parked = await _make_parked(runs[sid])
            t = asyncio.create_task(parked())
            tasks[sid] = t
            _register_claude_permission_refresh_task(sid, t)
        await asyncio.sleep(0)

        await teardown_all_claude_native_permission_refreshes()

        for sid in sessions:
            assert tasks[sid].cancelled(), f"{sid} not cancelled"
            assert runs[sid].cancelled is True, f"{sid} body did not observe cancel"
        assert _AUTO_CLAUDE_PERMISSION_REFRESH_TASKS == {}
    finally:
        await teardown_all_claude_native_permission_refreshes()
        for run in runs.values():
            await _drain([run])


# --------------------------------------------------------------------------- #
# Small helpers shared by the lifecycle tests (mirrors wake_forwarders fakes).
# --------------------------------------------------------------------------- #


@dataclass
class _ForwarderRun:
    """One parked refresh/forwarder stub run.

    :param task: The asyncio task executing this run.
    :param cancelled: True once the run observed CancelledError.
    """

    task: asyncio.Task[Any] | None = field(default=None)
    cancelled: bool = False


async def _drain(runs: list[_ForwarderRun]) -> None:
    """Cancel and await any still-parked stub runs."""
    leftovers = [r.task for r in runs if r.task is not None and not r.task.done()]
    for t in leftovers:
        t.cancel()
    if leftovers:
        await asyncio.wait(leftovers)
