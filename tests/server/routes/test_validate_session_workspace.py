"""
Tests that ``_validate_session_workspace`` forwards ``permission_store``
to ``resolve_host_owner``, so a non-owner can still be authorized when
the target host's owner is an admin.

The actual admin-bypass decision is exhaustively covered by
``resolve_host_owner``'s own tests
(``tests/server/routes/test_host_launch.py``); this test only pins the
plumbing — that ``_validate_session_workspace`` doesn't drop
``permission_store`` on the floor. ``validate_workspace`` (the real
host.stat round-trip) is stubbed out since it's out of scope here and
covered by ``tests/server/routes/test_workspace_validation.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request

from omnigent.server.routes import _workspace_validation
from omnigent.server.routes import sessions as sessions_module


class _FakeHost:
    def __init__(self, owner: str) -> None:
        self.host_id = "host_1"
        self.name = "admin-host"
        self.owner = owner


class _FakeHostStore:
    def __init__(self, host: _FakeHost) -> None:
        self._host = host

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self._host if host_id == self._host.host_id else None


class _FakePermissionStore:
    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


def _build_request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"})


@pytest.mark.asyncio
async def test_validate_session_workspace_allows_admin_owned_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(owner="admin@example.com")
    app = FastAPI()
    app.state.host_registry = object()  # unused: validate_workspace is stubbed below
    app.state.host_store = _FakeHostStore(host)
    request = _build_request(app)
    perms = _FakePermissionStore(admins={"admin@example.com"})

    async def _fake_validate_workspace(**kwargs: object) -> str:
        return "/tmp/canonical"

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    # agent_cache is a harmless non-None stub: _validate_session_workspace
    # has a pre-existing unconditional "agent_cache is None" guard raised
    # before resolve_host_owner is ever reached, orthogonal to the
    # permission_store plumbing under test here. Since bundle_location is
    # None, agent_cache.load() is never actually called.
    result = await sessions_module._validate_session_workspace(
        user_id="bob@example.com",
        host_id="host_1",
        workspace="/tmp/x",
        agent=SimpleNamespace(bundle_location=None),
        agent_cache=SimpleNamespace(),
        request=request,
        permission_store=perms,
    )
    assert result == "/tmp/canonical"


@pytest.mark.asyncio
async def test_validate_session_workspace_still_rejects_non_admin_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(owner="alice@example.com")
    app = FastAPI()
    app.state.host_registry = object()
    app.state.host_store = _FakeHostStore(host)
    request = _build_request(app)
    perms = _FakePermissionStore(admins={"admin@example.com"})  # alice is NOT admin

    async def _fake_validate_workspace(**kwargs: object) -> str:
        raise AssertionError("validate_workspace should not be reached for a rejected caller")

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await sessions_module._validate_session_workspace(
            user_id="bob@example.com",
            host_id="host_1",
            workspace="/tmp/x",
            agent=SimpleNamespace(bundle_location=None),
            agent_cache=SimpleNamespace(),
            request=request,
            permission_store=perms,
        )
    assert exc_info.value.status_code == 403
