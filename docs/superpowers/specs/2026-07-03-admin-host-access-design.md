# Design: regular users get access to admin-owned hosts

## Problem

`Host` records (`omnigent/stores/host_store.py`) are strictly single-owner:
the table's primary key is `(owner, name)`, and every non-destructive host
route enforces `host.owner != user_id → 403`. The `is_admin` flag does not
bypass this — an admin's host is invisible and unusable to every other user,
including other admins. There is no way today for a team to share one
machine (physical host or server-managed sandbox) registered under an admin
account.

## Goal

Any host owned by an `is_admin=True` user becomes visible and fully usable —
browse filesystem, create directories, launch a runner/session — to every
authenticated regular user, automatically. No per-host opt-in flag, no
per-user grant list: this is a blanket rule keyed only on the *host owner's*
admin flag.

## Non-goals

- Sharing a regular (non-admin) user's host with other users. Ownership of
  non-admin-owned hosts is unchanged.
- Letting a regular user delete, re-register, or re-credential an
  admin-owned host. Destructive/registration paths keep their existing
  strict owner-only checks.
- A generalized host-sharing/grant system (à la `PermissionStore` session
  grants). This is a fixed, admin-to-everyone rule, not a configurable ACL.
- Admin users being able to access *other admins'* hosts as if they were
  their own beyond the same everyone-gets-access rule (they get it too, same
  as any regular user — no special admin-to-admin path is added).

## Current state (for reference)

- `omnigent/stores/host_store.py::HostStore.list_hosts(owner)` — exact-owner
  query, used by `GET /v1/hosts`.
- `omnigent/server/routes/_host_launch.py::resolve_host_owner()` — the one
  shared ownership check, used by runner launch (`resolve_host_launch`) and
  the session-create workspace probe.
- `omnigent/server/routes/hosts.py` — three more inline copies of the same
  `if host.owner != user_id: raise HTTPException(403, "not your host")`
  check, in `get_host`, `list_host_filesystem` (via `_list_host_filesystem`),
  and `create_host_directory`. These duplicate `resolve_host_owner` instead
  of calling it.
- `permission_store.is_admin(user_id)` (`omnigent/stores/permission_store`)
  is the source of truth for the admin flag; `list_users()` returns
  `Account` objects (`id`, `is_admin`, ...) with no dedicated
  "list admin ids" method today.

## Design

### 1. Single authorization choke point

Extend `resolve_host_owner()` to accept the existing `permission_store`
(`PermissionStore | None`, already threaded through `hosts.py`'s router
and `resolve_host_launch`) and add an admin-owner bypass:

```python
def resolve_host_owner(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
    permission_store: PermissionStore | None = None,
) -> Host:
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if user_id is None or host.owner == user_id:
        return host
    if permission_store is not None and permission_store.is_admin(host.owner):
        return host
    raise HTTPException(status_code=403, detail="not your host")
```

Then replace the three duplicated inline checks in `hosts.py`
(`get_host`, `_list_host_filesystem`, `create_host_directory`) with calls to
this helper. This is a net deletion of duplicated logic, not just an
addition — today's four near-identical checks collapse to one definition,
removing the drift risk the `_host_launch.py` docstring already warns about
("the original bug was each site enforcing a different subset").

`resolve_host_launch` (runner launch) picks up the bypass automatically
since it already calls `resolve_host_owner`.

### 2. Listing

`GET /v1/hosts` must return the caller's own hosts unioned with every
admin's hosts. Add `HostStore.list_hosts_for_owners(owners: list[str])`,
mirroring the existing `online_host_ids` pattern — one
`WHERE owner IN (...)` query, ordered by `updated_at desc` — instead of one
`list_hosts()` call per admin.

In the route handler:

```python
owners = {user_id}
if permission_store is not None:
    owners.update(a.id for a in permission_store.list_users() if a.is_admin)
hosts = await asyncio.to_thread(host_store.list_hosts_for_owners, list(owners))
```

No new admin-enumeration method is needed — `permission_store.list_users()`
already returns each user's `is_admin` flag.

The `owner` field already included in each list/get response lets the UI
label a host as belonging to someone else if desired (out of scope to
change the UI here — the API already carries what it needs).

### 3. What does not change

- Destructive/registration paths — host deletion, tunnel
  re-registration/re-credentialing (`host_tunnel.py`,
  `register_managed_host`) — keep their current strict
  `host.owner == caller` checks. A regular user can use an admin's host but
  cannot delete or re-register it.
- Non-admin-owned hosts remain private to their owner exactly as today —
  the bypass only fires when the *host's* owner is an admin.
- Single-user/local (no auth, `user_id is None`) behavior is unchanged.

## Testing

- `resolve_host_owner`: non-admin caller + admin-owned host → allowed;
  non-admin caller + non-admin-owned host → still 403; caller is the host's
  own owner → allowed regardless of admin status (unchanged baseline).
- `GET /v1/hosts`: response includes the caller's own hosts plus every
  admin's hosts, with no duplicates when the caller is themselves an admin.
- Existing owner-check tests on `get_host`, filesystem browse/mkdir, and
  runner launch continue to pass unmodified (own-host access is untouched).
- Destructive/registration paths: no test changes expected, since they are
  untouched — a quick check that a non-owner still can't hit them confirms
  no regression.
