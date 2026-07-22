"""
Tests that validate_existing_host_workspace / _validate_session_workspace
forward permission_store to resolve_host_owner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request

from omnigent.server.routes import _session_create_validation as scv
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
async def test_validate_existing_host_workspace_allows_admin_owned_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(owner="admin@example.com")
    perms = _FakePermissionStore(admins={"admin@example.com"})

    async def _fake_validate_workspace(**kwargs: object) -> str:
        return "/tmp/canonical"

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    result = await scv.validate_existing_host_workspace(
        user_id="bob@example.com",
        host_id="host_1",
        workspace="/tmp/x",
        agent=SimpleNamespace(bundle_location=None),
        agent_cache=SimpleNamespace(),
        host_store=_FakeHostStore(host),
        host_registry=object(),
        permission_store=perms,
    )
    assert result == "/tmp/canonical"


@pytest.mark.asyncio
async def test_validate_existing_host_workspace_rejects_non_admin_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(owner="alice@example.com")
    perms = _FakePermissionStore(admins={"admin@example.com"})

    async def _fake_validate_workspace(**kwargs: object) -> str:
        raise AssertionError("validate_workspace should not be reached")

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await scv.validate_existing_host_workspace(
            user_id="bob@example.com",
            host_id="host_1",
            workspace="/tmp/x",
            agent=SimpleNamespace(bundle_location=None),
            agent_cache=SimpleNamespace(),
            host_store=_FakeHostStore(host),
            host_registry=object(),
            permission_store=perms,
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_validate_session_workspace_forwards_permission_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost(owner="admin@example.com")
    app = FastAPI()
    app.state.host_registry = object()
    app.state.host_store = _FakeHostStore(host)
    request = _build_request(app)
    perms = _FakePermissionStore(admins={"admin@example.com"})

    async def _fake_validate_workspace(**kwargs: object) -> str:
        return "/tmp/canonical"

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

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
