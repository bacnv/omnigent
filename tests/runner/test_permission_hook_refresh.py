"""Tests for claude-native permission hook auth refresh scheduling."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import app


def _jwt_payload(payload: object) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def _jwt_with_exp(exp: int | float | str) -> str:
    return _jwt_payload({"exp": exp})


def test_permission_hook_refresh_delay_uses_jwt_exp() -> None:
    token = _jwt_with_exp(1_700_001_800)

    delay = app._permission_hook_refresh_delay_s(token, now=1_700_000_000)

    assert delay == 1_500.0


def test_permission_hook_refresh_delay_never_busy_loops_for_expired_jwt() -> None:
    token = _jwt_with_exp(1_700_000_001)

    assert app._permission_hook_refresh_delay_s(token, now=1_700_000_000) == 60.0


def test_permission_hook_refresh_delay_falls_back_for_non_jwt() -> None:
    assert app._permission_hook_refresh_delay_s("opaque-token", now=1_700_000_000) == 1200.0


def test_permission_hook_refresh_delay_falls_back_for_none_token() -> None:
    assert app._permission_hook_refresh_delay_s(None, now=1_700_000_000) == 1200.0


def test_permission_hook_refresh_delay_falls_back_for_empty_token() -> None:
    assert app._permission_hook_refresh_delay_s("", now=1_700_000_000) == 1200.0


def test_permission_hook_refresh_delay_falls_back_for_malformed_jwt() -> None:
    assert app._permission_hook_refresh_delay_s("a.b!.c", now=1_700_000_000) == 1200.0
    assert app._permission_hook_refresh_delay_s("only-one-segment", now=1_700_000_000) == 1200.0
    assert app._permission_hook_refresh_delay_s("a..c", now=1_700_000_000) == 1200.0


def test_permission_hook_refresh_delay_falls_back_for_non_numeric_exp() -> None:
    token = _jwt_with_exp("soon")
    assert app._permission_hook_refresh_delay_s(token, now=1_700_000_000) == 1200.0


def test_permission_hook_refresh_delay_falls_back_for_non_dict_payload() -> None:
    now = 1_700_000_000
    assert app._permission_hook_refresh_delay_s(_jwt_payload([1, 2, 3]), now=now) == 1200.0
    assert app._permission_hook_refresh_delay_s(_jwt_payload("claims"), now=now) == 1200.0
    assert app._permission_hook_refresh_delay_s(_jwt_payload(42), now=now) == 1200.0


async def test_refresh_loop_propagates_fresh_token_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh loop mints via auth factory and writes headers from helper."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    written: list[dict[str, Any]] = []
    tokens = iter(["fresh-jwt-1"])

    def _auth_factory() -> str:
        return next(tokens)

    def _headers(server_url: str, *, bearer_token: str | None = None) -> dict[str, str]:
        assert server_url == "http://127.0.0.1:8000"
        assert bearer_token == "fresh-jwt-1"
        return {"Authorization": f"Bearer {bearer_token}", "X-Workspace-Id": "ws_1"}

    def _refresh(
        path: Path,
        *,
        ap_server_url: str,
        ap_auth_headers: dict[str, str],
    ) -> None:
        written.append(
            {
                "bridge_dir": path,
                "ap_server_url": ap_server_url,
                "ap_auth_headers": ap_auth_headers,
            }
        )

    delays = iter([0.0, 9999.0])

    monkeypatch.setattr(
        app, "_permission_hook_refresh_delay_s", lambda token, *, now=None: next(delays)
    )
    monkeypatch.setattr("omnigent.cli_auth.databricks_request_headers", _headers)
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_headers",
        _refresh,
    )

    task = asyncio.create_task(
        app._refresh_permission_hook_forever(
            bridge_dir=bridge_dir,
            server_url="http://127.0.0.1:8000",
            auth_factory=_auth_factory,
            initial_token="stale-token",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert written == [
        {
            "bridge_dir": bridge_dir,
            "ap_server_url": "http://127.0.0.1:8000",
            "ap_auth_headers": {
                "Authorization": "Bearer fresh-jwt-1",
                "X-Workspace-Id": "ws_1",
            },
        }
    ]


async def test_refresh_delay_recomputation_uses_fresh_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After each rewrite, the next sleep uses the newly minted token."""
    seen_tokens: list[str | None] = []
    mint_tokens = iter(["token-a", "token-b"])

    def _delay(token: str | None, *, now: float | None = None) -> float:
        seen_tokens.append(token)
        if len(seen_tokens) <= 2:
            return 0.0
        return 9999.0

    monkeypatch.setattr(app, "_permission_hook_refresh_delay_s", _delay)

    def _auth_factory() -> str:
        return next(mint_tokens)

    refresh_calls = 0

    def _refresh(*_a: Any, **_k: Any) -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_headers",
        _refresh,
    )
    monkeypatch.setattr(
        "omnigent.cli_auth.databricks_request_headers",
        lambda server_url, *, bearer_token=None: {"Authorization": f"Bearer {bearer_token}"},
    )

    task = asyncio.create_task(
        app._refresh_permission_hook_forever(
            bridge_dir=tmp_path,
            server_url="http://127.0.0.1:8000",
            auth_factory=_auth_factory,
            initial_token="initial",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert refresh_calls >= 1
    assert seen_tokens[0] == "initial"
    assert "token-a" in seen_tokens


async def test_refresh_fails_closed_when_auth_factory_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not overwrite a valid hook when mint yields no bearer."""
    written: list[Any] = []

    monkeypatch.setattr(app, "_permission_hook_refresh_delay_s", lambda token, *, now=None: 0.0)
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_headers",
        lambda *a, **k: written.append((a, k)),
    )
    monkeypatch.setattr(
        "omnigent.cli_auth.databricks_request_headers",
        lambda server_url, *, bearer_token=None: {},
    )

    with pytest.raises(RuntimeError, match="auth factory returned no token"):
        await app._refresh_permission_hook_forever(
            bridge_dir=tmp_path,
            server_url="http://127.0.0.1:8000",
            auth_factory=lambda: None,
            initial_token="stale",
        )

    assert written == []


async def test_auth_factory_mint_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking auth factory must not stall concurrent loop work."""
    import threading

    mint_entered = threading.Event()
    release_mint = threading.Event()
    concurrent_ticks = 0

    def _blocking_factory() -> str:
        mint_entered.set()
        # Block the worker thread; if this ran on the loop the ticker below
        # could not advance while we wait for release.
        if not release_mint.wait(timeout=2.0):
            raise TimeoutError("mint was not released")
        return "off-loop-token"

    delays = iter([0.0, 9999.0])
    monkeypatch.setattr(
        app, "_permission_hook_refresh_delay_s", lambda token, *, now=None: next(delays)
    )
    monkeypatch.setattr(
        "omnigent.cli_auth.databricks_request_headers",
        lambda server_url, *, bearer_token=None: {"Authorization": f"Bearer {bearer_token}"},
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_headers",
        lambda *_a, **_k: None,
    )

    async def _loop_ticker() -> None:
        nonlocal concurrent_ticks
        while not release_mint.is_set():
            concurrent_ticks += 1
            await asyncio.sleep(0.01)

    refresh_task = asyncio.create_task(
        app._refresh_permission_hook_forever(
            bridge_dir=tmp_path,
            server_url="http://127.0.0.1:8000",
            auth_factory=_blocking_factory,
            initial_token="stale",
        )
    )
    ticker_task = asyncio.create_task(_loop_ticker())

    assert await asyncio.to_thread(mint_entered.wait, 1.0)
    # While mint is blocked off-loop, the event loop must still schedule work.
    await asyncio.sleep(0.05)
    assert concurrent_ticks >= 2, concurrent_ticks
    release_mint.set()

    # Let refresh complete one cycle, then park on the next long delay.
    await asyncio.sleep(0.05)
    refresh_task.cancel()
    ticker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    with contextlib.suppress(asyncio.CancelledError):
        await ticker_task


async def test_refresh_fails_closed_when_auth_factory_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth if refresh is started without a factory."""
    written: list[Any] = []
    monkeypatch.setattr(app, "_permission_hook_refresh_delay_s", lambda token, *, now=None: 0.0)
    monkeypatch.setattr(
        "omnigent.claude_native_bridge.refresh_permission_hook_headers",
        lambda *a, **k: written.append((a, k)),
    )

    with pytest.raises(RuntimeError, match="no auth factory configured"):
        await app._refresh_permission_hook_forever(
            bridge_dir=tmp_path,
            server_url="http://127.0.0.1:8000",
            auth_factory=None,
            initial_token="stale",
        )

    assert written == []


async def test_no_auth_factory_runs_forwarder_without_refresh() -> None:
    """Auth-disabled / local path must not pair refresh (which would kill forwarder)."""
    ticks = 0

    async def _long_lived_forwarder() -> None:
        nonlocal ticks
        # Survive longer than the former 1200s fail-closed path would allow
        # in tests by just parking until cancelled externally — here we exit
        # cleanly after a few event-loop turns to prove we were not cancelled
        # by a sibling refresh task.
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0)

    await app._run_forwarder_and_permission_hook_refresh(_long_lived_forwarder(), None)

    assert ticks == 5


async def test_forwarder_and_hook_refresh_cancel_together() -> None:
    """Cancelling the combined task stops both forwarder and refresh children."""
    started = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    forwarder_cancelled = asyncio.Event()

    async def _fake_forwarder() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            forwarder_cancelled.set()
            raise

    async def _fake_refresh() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            raise

    task = asyncio.create_task(
        app._run_forwarder_and_permission_hook_refresh(_fake_forwarder(), _fake_refresh()),
        name="claude-forwarder-test",
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert forwarder_cancelled.is_set()
    assert refresh_cancelled.is_set()


async def test_refresh_error_cancels_and_awaits_forwarder_sibling() -> None:
    """Refresh failure cancels the forwarder sibling; error stays visible."""
    forwarder_cancelled = asyncio.Event()
    forwarder_finished = asyncio.Event()

    async def _fake_forwarder() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            forwarder_cancelled.set()
            raise
        finally:
            forwarder_finished.set()

    async def _fake_refresh() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("mint failed")

    with pytest.raises(Exception) as excinfo:
        await app._run_forwarder_and_permission_hook_refresh(
            _fake_forwarder(),
            _fake_refresh(),
        )

    assert forwarder_cancelled.is_set()
    assert forwarder_finished.is_set()
    err = excinfo.value
    # asyncio.TaskGroup raises ExceptionGroup (3.11+); keep the mint error visible.
    nested = list(getattr(err, "exceptions", ())) or [err]
    assert any(isinstance(item, RuntimeError) and "mint failed" in str(item) for item in nested)
