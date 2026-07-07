# Admin-Owned Host Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every authenticated regular user browse, create directories on, and launch runners/sessions on any host owned by an `is_admin=True` user, without touching destructive/registration paths or non-admin-owned hosts.

**Architecture:** One shared authorization function, `resolve_host_owner()` in `omnigent/server/routes/_host_launch.py`, already gates every non-destructive host route either directly or via `resolve_host_launch()`. Add a single admin-owner bypass there, thread the existing `permission_store` parameter to the two call sites that don't yet pass it, and collapse three duplicated inline copies of the same check in `hosts.py` to call the shared function instead. Separately, `GET /v1/hosts` needs its own change since it lists by owner rather than checking one: union the caller's own hosts with every admin's hosts via one new `HostStore.list_hosts_for_owners()` query.

**Tech Stack:** Python, FastAPI, SQLAlchemy (SQLite in tests), pytest + pytest-asyncio, httpx `ASGITransport` for integration tests.

## Global Constraints

- Bypass fires only when the **host's owner** is `is_admin=True` — an admin's own privileges don't extend to non-admin-owned hosts, and are not required for a user to access their *own* host (spec: "Non-goals").
- No per-host opt-in flag and no per-user grant list — every authenticated user gets access to every admin-owned host automatically (spec: "Goal").
- Destructive/registration paths (host deletion, tunnel re-registration/re-credentialing) keep their existing strict `host.owner == caller` checks, untouched by this plan (spec: "Non-goals", "What does not change").
- Single-user/local behavior (`user_id is None`) is unchanged — the bypass only evaluates when there's an authenticated caller (spec: "What does not change").

---

### Task 1: Admin-owner bypass in `resolve_host_owner`

**Files:**
- Modify: `omnigent/server/routes/_host_launch.py:49-79` (`resolve_host_owner`), `:120-124` (its call inside `resolve_host_launch`)
- Test: `tests/server/routes/test_host_launch.py`

**Interfaces:**
- Produces: `resolve_host_owner(*, user_id: str | None, host_id: str, host_store: HostStore, permission_store: PermissionStore | None = None) -> Host` — new optional `permission_store` kwarg, admin-owner bypass. Every later task that calls this function passes `permission_store` through.

- [ ] **Step 1: Write the failing tests**

Add to `tests/server/routes/test_host_launch.py`, inside `class TestResolveHostOwner` (after the existing `test_no_auth_skips_owner_check`):

```python
    def test_admin_owned_host_allowed_for_other_user(self) -> None:
        host = _FakeHost(host_id="host_1", owner="admin@example.com")
        store = _FakeHostStore(hosts={"host_1": host})
        perms = _FakePermissionStore(admins={"admin@example.com"})
        result = resolve_host_owner(
            user_id="alice",
            host_id="host_1",
            host_store=store,
            permission_store=perms,
        )
        assert result.host_id == "host_1"

    def test_non_admin_owned_host_still_403_with_permission_store(self) -> None:
        host = _FakeHost(host_id="host_1", owner="bob")
        store = _FakeHostStore(hosts={"host_1": host})
        perms = _FakePermissionStore(admins={"admin@example.com"})
        with pytest.raises(HTTPException) as exc_info:
            resolve_host_owner(
                user_id="alice",
                host_id="host_1",
                host_store=store,
                permission_store=perms,
            )
        assert exc_info.value.status_code == 403
```

Add the fake permission store next to the other fakes near the top of the file (after `_FakeConversationStore`):

```python
@dataclass
class _FakePermissionStore:
    admins: set[str] = field(default_factory=set)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admins
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/server/routes/test_host_launch.py -v -k "admin_owned or still_403_with_permission_store"`
Expected: FAIL — `resolve_host_owner() got an unexpected keyword argument 'permission_store'`

- [ ] **Step 3: Implement the bypass**

In `omnigent/server/routes/_host_launch.py`, replace `resolve_host_owner`:

```python
def resolve_host_owner(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
    permission_store: PermissionStore | None = None,
) -> Host:
    """
    Authorize that the caller may act on a known host.

    Every route that reaches a host on the caller's behalf must pass
    this first so the owner check can't drift between them: the runner
    launch (via :func:`resolve_host_launch`) AND the session-create
    workspace probe, which sends a ``host.stat`` to the host. The
    original bug had that probe contacting another user's host before
    any ownership check. When ``user_id`` is ``None`` (auth disabled)
    the check is skipped, consistent with single-user/local behavior.

    A caller who does not own the host is still authorized when the
    host's owner is an admin (``permission_store.is_admin(host.owner)``)
    — admin-owned hosts are usable (not just visible) by every
    authenticated user. This does not touch destructive/registration
    paths, which call their own owner-only checks directly instead of
    this function.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations.
    :param permission_store: Used to check whether the host's owner is
        an admin. ``None`` disables the admin bypass (falls back to
        strict ownership), consistent with other auth-disabled paths.
    :returns: The host record the caller is authorized to use.
    :raises HTTPException: 404 if the host is unknown; 403 if it is
        owned by a different, non-admin user.
    """
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if user_id is None or host.owner == user_id:
        return host
    if permission_store is not None and permission_store.is_admin(host.owner):
        return host
    raise HTTPException(status_code=403, detail="not your host")
```

Then in `resolve_host_launch` (same file), update its internal call to forward the parameter it already receives:

```python
    host = resolve_host_owner(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        permission_store=permission_store,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/server/routes/test_host_launch.py -v`
Expected: PASS (all tests in the file, including the two new ones and the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/_host_launch.py tests/server/routes/test_host_launch.py
git commit -m "feat(hosts): add admin-owner bypass to resolve_host_owner"
```

---

### Task 2: Thread the bypass through session-create's host probe

**Files:**
- Modify: `omnigent/server/routes/sessions.py:5920-5928` (`_validate_session_workspace` signature), `:6002-6013` (its `resolve_host_owner` call), `:12176-12183` (its one caller, `_create_session_from_existing_agent`)
- Test: `tests/server/routes/test_validate_session_workspace.py` (new file)

**Interfaces:**
- Consumes: `resolve_host_owner(*, user_id, host_id, host_store, permission_store=None)` from Task 1.
- Produces: `_validate_session_workspace(..., permission_store: PermissionStore | None = None) -> str` — new optional kwarg. No other caller exists today (verified via `grep -n "_validate_session_workspace(" omnigent/server/routes/sessions.py`), so this is the only call site to update.

- [ ] **Step 1: Write the failing test**

Create `tests/server/routes/test_validate_session_workspace.py`:

```python
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

    result = await sessions_module._validate_session_workspace(
        user_id="bob@example.com",
        host_id="host_1",
        workspace="/tmp/x",
        agent=SimpleNamespace(bundle_location=None),
        agent_cache=None,
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
            agent_cache=None,
            request=request,
            permission_store=perms,
        )
    assert exc_info.value.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/server/routes/test_validate_session_workspace.py -v`
Expected: FAIL — `_validate_session_workspace() got an unexpected keyword argument 'permission_store'`

- [ ] **Step 3: Thread the parameter through**

In `omnigent/server/routes/sessions.py`, update the `_validate_session_workspace` signature (around line 5920):

```python
async def _validate_session_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    request: Request,
    permission_store: PermissionStore | None = None,
) -> str:
```

Add one line to its docstring's `:param` list (after the `:param request:` line):

```python
    :param permission_store: Used by :func:`resolve_host_owner` to allow
        access when the target host's owner is an admin. ``None``
        disables the admin bypass.
```

Update its `resolve_host_owner` call (around line 6007):

```python
    if host_store_inst is not None:
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store_inst,
            permission_store=permission_store,
        )
        host_name = host.name
```

Update its one caller, `_create_session_from_existing_agent` (around line 12176), which already receives `permission_store` as a parameter:

```python
    canonical_workspace: str | None = body.workspace
    if body.host_id is not None:
        canonical_workspace = await _validate_session_workspace(
            user_id=user_id,
            host_id=body.host_id,
            workspace=body.workspace,
            agent=agent,
            agent_cache=agent_cache,
            request=request,
            permission_store=permission_store,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/server/routes/test_validate_session_workspace.py -v`
Expected: PASS

Then check nothing regressed on the existing rejection contract:

Run: `pytest tests/server/integration/test_sessions_permissions.py -v -k "rejects_other_users_host or worktree_session_on_alice_host"`
Expected: PASS (these tests don't wire admin status for alice, so `permission_store.is_admin("alice@example.com")` is `False` and the 403 is unchanged)

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/sessions.py tests/server/routes/test_validate_session_workspace.py
git commit -m "feat(hosts): forward permission_store to session-create host check"
```

---

### Task 3: Collapse duplicated owner checks in `hosts.py` to use `resolve_host_owner`

**Files:**
- Modify: `omnigent/server/routes/hosts.py:43` (import), `:384-388` (`get_host`), `:782-791` (`_list_host_filesystem`), `:866-872` (`create_host_directory`)
- Test: `tests/server/integration/test_hosts_api.py` (extend `multi_user_app` tests), `tests/server/integration/test_hosts_filesystem.py` (extend)

**Interfaces:**
- Consumes: `resolve_host_owner(*, user_id, host_id, host_store, permission_store=None) -> Host` from Task 1, raising `HTTPException(404)` / `HTTPException(403)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/server/integration/test_hosts_api.py`, after `test_get_host_403_wrong_owner`:

```python
async def test_get_host_admin_owned_allowed_for_other_user(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    A regular user can GET an admin-owned host's details.

    If this 403s, the admin bypass isn't wired into get_host.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_admin1", "admin-laptop", "admin@test.com")
    app.state.permission_store.ensure_user("admin@test.com")
    app.state.permission_store.set_admin("admin@test.com", True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/hosts/host_admin1",
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 200, (
        f"Expected 200 for admin-owned host, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["owner"] == "admin@test.com"
```

Add to the same file, after `test_launch_runner_403_wrong_owner`:

```python
async def test_launch_runner_admin_owned_allowed_for_other_user(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    A regular user can launch a runner on an admin-owned host.

    If this 403s, the admin bypass isn't reaching resolve_host_launch.
    """
    app, registry, host_store, conv_store = multi_user_app
    host_store.upsert_on_connect("host_admin2", "admin-laptop", "admin@test.com")
    app.state.permission_store.ensure_user("admin@test.com")
    app.state.permission_store.set_admin("admin@test.com", True)
    from omnigent.host.frames import HostHelloFrame

    registry.register(
        "host_admin2",
        type(
            "FakeWS",
            (),
            {"send_text": lambda self, d: None, "receive_text": lambda self: ""},
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="admin-laptop"),
        owner="admin@test.com",
    )
    # Bob owns the session he's binding to (resolve_host_launch also
    # checks session ownership, independent of host ownership).
    from omnigent.server.auth import LEVEL_OWNER

    conv = conv_store.create_conversation(agent_id=None)
    app.state.permission_store.ensure_user("bob@test.com")
    app.state.permission_store.grant("bob@test.com", conv.id, LEVEL_OWNER)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_admin2/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 200, (
        f"Expected 200 for admin-owned host launch, got {resp.status_code}: {resp.text}"
    )
```

Add to `tests/server/integration/test_hosts_filesystem.py`, after `test_list_filesystem_owner_check_blocks_other_users`:

```python
async def test_list_filesystem_admin_owned_host_allowed_for_other_user(
    fs_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    db_uri: str,
) -> None:
    """
    A regular user CAN browse an admin-owned host's filesystem.

    Companion to ``test_list_filesystem_owner_check_blocks_other_users``:
    that test pins the non-admin case still 403ing; this one pins the
    new admin-owner bypass. Alice owns the host and is an admin, bob
    is a regular user browsing it.

    ``db_uri`` is requested directly (rather than reaching into
    ``fs_app``'s ``host_store``) to build a second store handle on the
    same on-disk SQLite file — the same pattern ``perm_store`` /
    ``conv_store`` fixtures use elsewhere (see
    ``tests/server/routes/test_auth_helpers.py``).
    """
    from omnigent.server.auth import AuthProvider
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    _app, _reg, host_store, conv_store = fs_app

    class _Stub(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return request.headers.get("X-Test-User")

    auth = _Stub()
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user("alice@example.com")
    permission_store.set_admin("alice@example.com", True)

    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth),
        prefix="/v1",
    )
    auth_app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            auth_provider=auth,
            permission_store=permission_store,
        ),
        prefix="/v1",
    )

    host_store.upsert_on_connect(
        host_id="host_alice_admin",
        name="alice-laptop",
        owner="alice@example.com",
    )

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/hosts/host_alice_admin/filesystem",
            headers={"X-Test-User": "bob@example.com"},
        )
    # 409 (host offline) is acceptable here — no mock host is connected
    # in this test, only the ownership gate is under test. 403 would mean
    # the bypass didn't fire.
    assert resp.status_code != 403, (
        f"Expected the admin-owner bypass to pass ownership, got 403: {resp.text}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/server/integration/test_hosts_api.py -v -k admin_owned_allowed`
Run: `pytest tests/server/integration/test_hosts_filesystem.py -v -k admin_owned_host_allowed`
Expected: FAIL — both get 403 (the inline checks don't know about admins yet)

- [ ] **Step 3: Replace the three inline checks**

In `omnigent/server/routes/hosts.py`, update the import (line 43):

```python
from omnigent.server.routes._host_launch import resolve_host_launch, resolve_host_owner
```

In `get_host` (replace the block at lines 384-388):

```python
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store,
            permission_store=permission_store,
        )
```

(This replaces both the `host_store.get_host` call and the two `if` checks that followed it — `resolve_host_owner` does the lookup itself.)

In `_list_host_filesystem` (replace the block at lines 782-791, keeping the two prior comment lines' intent folded into the call):

```python
        user_id = require_user(request, auth_provider)

        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store,
            permission_store=permission_store,
        )
```

In `create_host_directory` (replace the block at lines 866-872):

```python
        user_id = require_user(request, auth_provider)

        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store,
            permission_store=permission_store,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/server/integration/test_hosts_api.py -v`
Run: `pytest tests/server/integration/test_hosts_filesystem.py -v`
Run: `pytest tests/server/integration/test_hosts_create_directory.py -v`
Expected: PASS — all pre-existing tests (including the pinned `test_get_host_403_wrong_owner`, `test_launch_runner_403_wrong_owner`, `test_list_filesystem_owner_check_blocks_other_users`, which don't wire an admin, must still pass unchanged) plus the new ones

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/hosts.py tests/server/integration/test_hosts_api.py tests/server/integration/test_hosts_filesystem.py
git commit -m "refactor(hosts): route get_host/filesystem/mkdir through resolve_host_owner"
```

---

### Task 4: `HostStore.list_hosts_for_owners`

**Files:**
- Modify: `omnigent/stores/host_store.py` (add method after `list_hosts`, currently ending at line 507)
- Test: `tests/stores/test_host_store.py`

**Interfaces:**
- Produces: `HostStore.list_hosts_for_owners(owners: list[str]) -> list[Host]` — union query across multiple owners, ordered by `updated_at` descending, empty list for empty input.

- [ ] **Step 1: Write the failing tests**

Add to `tests/stores/test_host_store.py`, after `test_list_hosts_empty_for_unknown_owner`:

```python
def test_list_hosts_for_owners_unions_multiple_owners(
    host_store: HostStore,
) -> None:
    """
    Verify list_hosts_for_owners returns hosts across all given owners.

    If it only returns one owner's hosts, the IN-clause is broken or
    missing.
    """
    host_store.upsert_on_connect("host_u1", "alice-laptop", "alice@example.com")
    host_store.upsert_on_connect("host_u2", "bob-laptop", "bob@example.com")
    host_store.upsert_on_connect("host_u3", "carol-laptop", "carol@example.com")

    result = host_store.list_hosts_for_owners(["alice@example.com", "bob@example.com"])

    host_ids = {h.host_id for h in result}
    assert host_ids == {"host_u1", "host_u2"}


def test_list_hosts_for_owners_dedupes_when_same_owner_repeated(
    host_store: HostStore,
) -> None:
    """
    Verify passing the same owner twice doesn't duplicate its hosts.

    Covers the caller-is-also-admin case in the /v1/hosts route, where
    the owners set may still contain a duplicate before de-duplication.
    """
    host_store.upsert_on_connect("host_u4", "alice-laptop", "alice@example.com")

    result = host_store.list_hosts_for_owners(["alice@example.com", "alice@example.com"])

    assert [h.host_id for h in result] == ["host_u4"]


def test_list_hosts_for_owners_empty_input_returns_empty(
    host_store: HostStore,
) -> None:
    """
    Verify an empty owners list returns an empty result without
    touching the database.
    """
    assert host_store.list_hosts_for_owners([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/stores/test_host_store.py -v -k list_hosts_for_owners`
Expected: FAIL — `AttributeError: 'HostStore' object has no attribute 'list_hosts_for_owners'`

- [ ] **Step 3: Implement the method**

In `omnigent/stores/host_store.py`, add after `list_hosts` (after line 507, before `get_host`):

```python
    def list_hosts_for_owners(self, owners: list[str]) -> list[Host]:
        """
        List all hosts owned by any of the given users.

        Bulk variant of :meth:`list_hosts` for merging a caller's own
        hosts with every admin's hosts in one query — ``GET /v1/hosts``
        exposes admin-owned hosts to every user, so it needs one
        ``WHERE owner IN (...)`` instead of one :meth:`list_hosts` call
        per admin.

        :param owners: User IDs to filter by, e.g.
            ``["alice@example.com", "admin@example.com"]``. Duplicates
            are tolerated (deduped via the IN-clause); empty input
            returns an empty list without touching the database.
        :returns: List of :class:`Host` entities across all given
            owners, ordered by ``updated_at`` descending.
        """
        if not owners:
            return []
        with self._session() as session:
            rows = (
                session.query(SqlHost)
                .filter(SqlHost.owner.in_(set(owners)))
                .order_by(SqlHost.updated_at.desc())
                .all()
            )
            return [_row_to_host(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/stores/test_host_store.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add omnigent/stores/host_store.py tests/stores/test_host_store.py
git commit -m "feat(hosts): add HostStore.list_hosts_for_owners"
```

---

### Task 5: `GET /v1/hosts` lists admin-owned hosts alongside the caller's own

**Files:**
- Modify: `omnigent/server/routes/hosts.py:319-336` (`list_hosts` handler)
- Test: `tests/server/integration/test_hosts_api.py` (extend `multi_user_app` tests)

**Interfaces:**
- Consumes: `HostStore.list_hosts_for_owners(owners: list[str]) -> list[Host]` from Task 4; `PermissionStore.list_users() -> list[Account]` (existing, `Account.id: str`, `Account.is_admin: bool`).

- [ ] **Step 1: Write the failing test**

Add to `tests/server/integration/test_hosts_api.py`, after `test_list_hosts_filters_by_owner`:

```python
async def test_list_hosts_includes_admin_owned_hosts(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    GET /v1/hosts returns the caller's own hosts plus every admin's
    hosts, with no duplicate when the caller is themselves an admin.

    If bob doesn't see admin@test.com's host, the union query is
    missing or the admin lookup is broken. If admin@test.com's own
    list has a duplicate, the owners set isn't deduped.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_bob_own", "bob-laptop", "bob@test.com")
    host_store.upsert_on_connect("host_admin_own", "admin-laptop", "admin@test.com")
    app.state.permission_store.ensure_user("admin@test.com")
    app.state.permission_store.set_admin("admin@test.com", True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A regular user sees their own host plus the admin's.
        resp = await client.get("/v1/hosts", headers={"x-test-user": "bob@test.com"})
        assert resp.status_code == 200
        host_ids = {h["host_id"] for h in resp.json()["hosts"]}
        assert host_ids == {"host_bob_own", "host_admin_own"}, (
            f"Bob should see his own host plus the admin's, got {host_ids}."
        )

        # The admin sees their own host exactly once (not duplicated).
        resp = await client.get("/v1/hosts", headers={"x-test-user": "admin@test.com"})
        assert resp.status_code == 200
        admin_hosts = resp.json()["hosts"]
        assert [h["host_id"] for h in admin_hosts] == ["host_admin_own"], (
            f"Admin's own-host listing should have no duplicates, got {admin_hosts}."
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/server/integration/test_hosts_api.py -v -k test_list_hosts_includes_admin_owned_hosts`
Expected: FAIL — bob's listing only contains `host_bob_own` (admin's host missing)

- [ ] **Step 3: Union owners in the route handler**

In `omnigent/server/routes/hosts.py`, replace the body of `list_hosts` (lines 331-335):

```python
        user_id = require_user(request, auth_provider)
        if user_id is None:
            hosts = await asyncio.to_thread(host_store.list_hosts, "local")
        else:
            owners = {user_id}
            if permission_store is not None:
                accounts = await asyncio.to_thread(permission_store.list_users)
                owners.update(a.id for a in accounts if a.is_admin)
            hosts = await asyncio.to_thread(host_store.list_hosts_for_owners, list(owners))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/server/integration/test_hosts_api.py -v`
Expected: PASS — all tests in the file, including `test_list_hosts_filters_by_owner` (still passes: neither alice nor bob is an admin there, so `owners` is just `{user_id}`) and the new test

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/hosts.py tests/server/integration/test_hosts_api.py
git commit -m "feat(hosts): GET /v1/hosts includes admin-owned hosts for every user"
```

---

### Task 6: Full regression pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full host and session test suites**

Run: `pytest tests/stores/test_host_store.py tests/server/routes/test_host_launch.py tests/server/routes/test_validate_session_workspace.py tests/server/integration/test_hosts_api.py tests/server/integration/test_hosts_filesystem.py tests/server/integration/test_hosts_create_directory.py tests/server/integration/test_hosts_management_e2e.py tests/server/integration/test_host_session_binding.py tests/server/integration/test_sessions_permissions.py -v`
Expected: PASS — no regressions across the full set of files this plan touched or could affect

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: PASS (or auto-fixed); re-stage and re-run if it modifies files

- [ ] **Step 3: Commit any pre-commit fixups**

```bash
git add -u
git commit -m "chore: pre-commit fixups"
```

(Skip this step entirely if pre-commit made no changes.)
