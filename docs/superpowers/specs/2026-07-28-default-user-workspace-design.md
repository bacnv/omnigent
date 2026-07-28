# Default User Workspace for New Sessions

## Motivation

New external-host sessions currently require the user to choose a workspace. New
sessions should start in a user-specific directory on the selected host while
keeping explicit workspace choices intact.

## Requirements

- New external-host sessions default to Claude Code native.
- The default Claude model is the version-agnostic `sonnet` alias.
- The default Claude effort is `high`.
- The default workspace is `<OS home>/<Omnigent username>`:
  - OS user `claude`, Omnigent user `bacnv`: `/home/claude/bacnv`
  - OS user `root`, Omnigent user `bacnv`: `/root/bacnv`
- The Omnigent username is the authenticated identity, not the OS username.
- Only the existing normalized username slug is accepted when constructing the
  path; it must not permit path traversal.
- The default directory is created when it does not exist.
- Existing explicit, project-prefilled, recent, or otherwise user-selected
  workspaces always take precedence over the default.
- Managed sandbox sessions retain their current workspace behavior.
- Existing sessions are unchanged.

## Design

The change is implemented in the New Chat flow using the existing host
filesystem APIs. After an external host is selected and its home directory is
resolved, the UI resolves the current authenticated Omnigent identity, builds
`<home>/<username>`, and seeds the workspace only when no higher-priority value
has been supplied. The UI uses the existing directory-create endpoint before
launching a session when the selected default directory is absent.

The session API contract, host protocol, database schema, and managed sandbox
provisioning are unchanged.

New-session defaults are represented by the existing native-agent, model, and
effort controls. A user-selected model or effort overrides `sonnet` / `high`.
The create request carries the resulting native launch configuration through
the existing fields.

## Data flow

1. Select an external host.
2. Resolve the host home directory from the existing filesystem root listing.
3. Resolve the authenticated Omnigent username from the existing identity
   mechanism.
4. Validate the username as a safe single path segment.
5. Seed `<home>/<username>` only if the workspace is empty and no project or
   recent workspace has won.
6. On submit, create the directory through the existing host directory API if
   necessary.
7. Submit the normal session-create request with Claude native, `sonnet`,
   `high`, and the resolved workspace.

## Error handling

- Unresolved identity or an invalid username stops default-path construction
  rather than producing a potentially unsafe path.
- An offline host or filesystem error stops creation and surfaces the existing
  error; the flow does not silently fall back to another directory.
- A directory that already exists is treated as success, including a concurrent
  create race.
- A user-supplied workspace is never overwritten by default seeding.

## Testing

Add focused unit/component coverage for:

- Home/username joining for `/home/claude/bacnv` and `/root/bacnv`.
- Rejection of unsafe usernames.
- Claude native, `sonnet`, and `high` defaults.
- Creation of a missing default directory and reuse of an existing one.
- Precedence of explicit, project-prefilled, and recent workspaces.
- Directory-create failures preventing session creation.

Run the relevant web tests and lint/type checks through the repository's normal
`just`/package scripts.
