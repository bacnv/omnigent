"""Integration tests for the scheduled-tasks CRUD routes.

Uses a real ``SqlAlchemyScheduledTaskStore`` + ``SqlAlchemyPermissionStore`` so
the full request → store → response pipeline is exercised, including RRULE
validation (400s) and live-scheduler sync on every mutation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as server_app
from omnigent.server.app import create_app
from omnigent.server.routes import scheduled_tasks as scheduled_tasks_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_host_workspace_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _validate_workspace(**kwargs: object) -> str:
        workspace = kwargs["workspace"]
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            from omnigent.errors import ErrorCode, OmnigentError

            raise OmnigentError(
                "workspace must be an absolute path starting with /",
                code=ErrorCode.INVALID_INPUT,
            )
        return workspace

    monkeypatch.setattr(
        scheduled_tasks_routes,
        "validate_existing_host_workspace",
        _validate_workspace,
    )


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    # Enter the lifespan so app.state.scheduled_task_scheduler exists and the
    # routes can sync to it.
    async with auth_app.router.lifespan_context(auth_app):
        transport = httpx.ASGITransport(app=auth_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


def _headers(email: str = "alice@example.com") -> dict[str, str]:
    return {"X-Forwarded-Email": email}


def _make_user(db_uri: str, email: str = "alice@example.com") -> None:
    SqlAlchemyPermissionStore(db_uri).ensure_user(email, is_admin=False)


_VALID_RRULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"


def _create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "nightly triage",
        "prompt": "triage the queue",
        "rrule": _VALID_RRULE,
        "agent_id": builtin_agent_id(server_app._CLAUDE_NATIVE_AGENT_NAME),
        "timezone": "America/Los_Angeles",
        "workspace": "/repo",
        "host_id": "4b653f6031f35d168cc0b37caa1306d1",
    }
    body.update(overrides)
    return body


async def test_create_lists_and_gets(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    resp = await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["name"] == "nightly triage"
    assert created["rrule"] == _VALID_RRULE
    assert created["owner_user_id"] == "alice@example.com"
    assert created["workspace"] == "/repo"
    assert created["host_id"] == "4b653f6031f35d168cc0b37caa1306d1"
    assert "base_branch" not in created
    assert "execution_target" not in created
    task_id = created["id"]

    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers())
    assert listed.status_code == 200
    ids = [t["id"] for t in listed.json()["scheduled_tasks"]]
    assert task_id in ids

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["id"] == task_id


async def test_create_rejects_invalid_rrule(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    # FREQ=SECONDLY fires far below the 1-hour floor.
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(rrule="FREQ=SECONDLY"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_rejects_unknown_agent(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(agent_id="missing_agent"),
        headers=_headers(),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("model_override", ["--danger", "bad model"])
async def test_create_rejects_invalid_model_override(
    auth_client: httpx.AsyncClient, db_uri: str, model_override: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(model_override=model_override),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_rejects_invalid_reasoning_effort(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(reasoning_effort="extreme"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_rejects_relative_workspace(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(workspace="relative/path"),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


async def test_create_rejects_missing_connected_host_inputs(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(host_id=None),
        headers=_headers(),
    )
    assert resp.status_code == 422, resp.text


async def test_create_rejects_unsupported_public_fields(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(base_branch="main", execution_target="managed_sandbox"),
        headers=_headers(),
    )
    assert resp.status_code == 422, resp.text


async def _wire_real_host_ownership(
    *,
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    host_owner: str,
    host_id: str,
    admin: bool,
) -> None:
    """Undo the autouse workspace stub and exercise real host ownership."""
    from omnigent.server.routes import _session_create_validation as scv
    from omnigent.server.routes import _workspace_validation
    from omnigent.stores.host_store import HostStore

    # Restore the real ownership gate used by create/update.
    monkeypatch.setattr(
        scheduled_tasks_routes,
        "validate_existing_host_workspace",
        scv.validate_existing_host_workspace,
    )

    async def _fake_validate_workspace(**kwargs: object) -> str:
        workspace = kwargs["workspace"]
        assert isinstance(workspace, str)
        return workspace

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(host_id, "sched-host", host_owner)
    auth_app.state.host_store = host_store
    auth_app.state.host_registry = object()

    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(host_owner)
    if admin:
        perms.set_admin(host_owner, True)
    # Caller is a regular user.
    perms.ensure_user("alice@example.com")


async def test_create_allows_admin_owned_host(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
    await _wire_real_host_ownership(
        auth_app=auth_app,
        auth_client=auth_client,
        db_uri=db_uri,
        monkeypatch=monkeypatch,
        host_owner="admin@example.com",
        host_id=host_id,
        admin=True,
    )
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(host_id=host_id),
        headers=_headers("alice@example.com"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner_user_id"] == "alice@example.com"
    assert resp.json()["host_id"] == host_id


async def test_create_rejects_regular_cross_owner_host(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
    await _wire_real_host_ownership(
        auth_app=auth_app,
        auth_client=auth_client,
        db_uri=db_uri,
        monkeypatch=monkeypatch,
        host_owner="bob@example.com",
        host_id=host_id,
        admin=False,
    )
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(host_id=host_id),
        headers=_headers("alice@example.com"),
    )
    assert resp.status_code == 403, resp.text


async def test_update_allows_admin_owned_host(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Create under the autouse stub first (no host ownership), then switch
    # ownership wiring and patch host_id to an admin-owned host.
    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(),
            headers=_headers("alice@example.com"),
        )
    ).json()
    host_id = "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"
    await _wire_real_host_ownership(
        auth_app=auth_app,
        auth_client=auth_client,
        db_uri=db_uri,
        monkeypatch=monkeypatch,
        host_owner="admin@example.com",
        host_id=host_id,
        admin=True,
    )
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"host_id": host_id, "workspace": "/repo"},
        headers=_headers("alice@example.com"),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["host_id"] == host_id
    assert patched.json()["owner_user_id"] == "alice@example.com"


async def test_update_rejects_regular_cross_owner_host(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks",
            json=_create_body(),
            headers=_headers("alice@example.com"),
        )
    ).json()
    host_id = "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"
    await _wire_real_host_ownership(
        auth_app=auth_app,
        auth_client=auth_client,
        db_uri=db_uri,
        monkeypatch=monkeypatch,
        host_owner="bob@example.com",
        host_id=host_id,
        admin=False,
    )
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"host_id": host_id, "workspace": "/repo"},
        headers=_headers("alice@example.com"),
    )
    assert patched.status_code == 403, patched.text


async def test_update_changes_fields_and_validates_rrule(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    # Valid partial update.
    patched = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"name": "renamed", "state": "paused"},
        headers=_headers(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "renamed"
    assert patched.json()["state"] == "paused"

    # Invalid rrule on update is a 400.
    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"rrule": "FREQ=SECONDLY"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text

    # Deletion is a DELETE operation, not an arbitrary PATCH state.
    deleted_state = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"state": "deleted"},
        headers=_headers(),
    )
    assert deleted_state.status_code == 422, deleted_state.text


async def test_update_rejects_invalid_model_override(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"model_override": "--danger"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


async def test_update_rejects_invalid_reasoning_effort(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{created['id']}",
        json={"reasoning_effort": "extreme"},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


async def test_delete_removes_task(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    deleted = await auth_client.delete(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert deleted.status_code == 200, deleted.text

    got = await auth_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_headers())
    assert got.status_code == 404


async def test_other_users_task_is_not_visible(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    _make_user(db_uri, "alice@example.com")
    _make_user(db_uri, "bob@example.com")
    created = (
        await auth_client.post(
            "/v1/scheduled-tasks", json=_create_body(), headers=_headers("alice@example.com")
        )
    ).json()
    task_id = created["id"]

    # Bob cannot see or fetch Alice's task.
    got = await auth_client.get(
        f"/v1/scheduled-tasks/{task_id}", headers=_headers("bob@example.com")
    )
    assert got.status_code == 404
    listed = await auth_client.get("/v1/scheduled-tasks", headers=_headers("bob@example.com"))
    assert listed.json()["scheduled_tasks"] == []


@pytest.mark.parametrize("tz", ["Not/A_Timezone", "", "../UTC"])
async def test_create_rejects_invalid_timezone(
    auth_client: httpx.AsyncClient, db_uri: str, tz: str
) -> None:
    _make_user(db_uri)
    resp = await auth_client.post(
        "/v1/scheduled-tasks",
        json=_create_body(timezone=tz),
        headers=_headers(),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("tz", ["Bogus/Zone", "", "../UTC"])
async def test_update_rejects_invalid_timezone(
    auth_client: httpx.AsyncClient, db_uri: str, tz: str
) -> None:
    _make_user(db_uri)
    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    task_id = created["id"]

    bad = await auth_client.patch(
        f"/v1/scheduled-tasks/{task_id}",
        json={"timezone": tz},
        headers=_headers(),
    )
    assert bad.status_code == 400, bad.text


async def test_scheduler_synced_on_create_and_delete(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, db_uri: str
) -> None:
    _make_user(db_uri)
    scheduler = auth_app.state.scheduled_task_scheduler
    before = scheduler.job_count

    created = (
        await auth_client.post("/v1/scheduled-tasks", json=_create_body(), headers=_headers())
    ).json()
    assert scheduler.job_count == before + 1

    await auth_client.delete(f"/v1/scheduled-tasks/{created['id']}", headers=_headers())
    assert scheduler.job_count == before
