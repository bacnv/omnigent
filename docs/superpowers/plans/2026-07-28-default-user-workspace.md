# Default User Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new external-host sessions default to `<OS home>/<authenticated Omnigent username>` with Claude Code native, Sonnet, and high effort, creating the directory when needed.

**Architecture:** Keep the existing `/v1/sessions` contract and host filesystem protocol. Extend the web New Chat flow with small pure helpers for safe username/path derivation, seed the default only after host home and identity are available, and create the selected default directory immediately before session creation through the existing `createHostDirectory` API.

**Tech Stack:** React, TypeScript, TanStack Query, existing host filesystem/session APIs, Vitest, ESLint/Oxlint.

## Global Constraints

- Default workspace is `<OS home>/<Omnigent username>`; the username is authenticated identity, not OS username.
- `/home/claude/bacnv` and `/root/bacnv` are required examples.
- No `-omnigent` suffix.
- Default harness is Claude Code native; default model is `sonnet`; default effort is `high`.
- Explicit, project-prefilled, recent, and manually selected workspaces/options override defaults.
- Managed sandbox behavior and existing sessions remain unchanged.
- Reject unsafe username path segments; do not allow path traversal.
- Directory/filesystem errors stop session creation; never silently fall back to another path.
- Do not add a dependency or change the API/database/host protocol.

---

## File map

- Modify `web/src/shell/NewChatDialog.tsx`: default agent/model/effort selection, workspace seeding, and pre-submit directory creation.
- Modify `web/src/hooks/useHostFilesystem.ts`: expose the existing directory-create function to New Chat without duplicating HTTP logic.
- Modify `web/src/lib/identity.ts` only if the current identity cache needs a React-safe readiness signal; prefer using the existing `getCurrentUserId()` and existing app identity resolution before adding state.
- Test `web/src/shell/NewChatDialog.test.tsx` or `web/src/shell/NewChatDialog.flow.test.tsx`: UI/default/precedence/request behavior.
- Test `web/src/hooks/useHostFilesystem.test.ts` if helper/API coverage is needed; otherwise keep tests in the existing New Chat suites.

## Interfaces

- Reuse `deriveHomeDir(entries: HostFilesystemEntry[]): string | null` and `useHostFilesystem(hostId, path)`.
- Reuse/export `createHostDirectory(hostId: string, path: string): Promise<string>` from `web/src/hooks/useHostFilesystem.ts`.
- Add pure helpers in `NewChatDialog.tsx` only if useful:
  - `isSafeUsernameSegment(username: string): boolean`
  - `defaultUserWorkspace(home: string, username: string): string | null`
- The create flow must call `createHostDirectory(selectedHostId, workspaceTrimmed)` before the existing `POST /v1/sessions` or bundled-session launch, only for the selected default directory and only for external hosts.

## Task 1: Add tested safe workspace derivation

**Files:**
- Modify: `web/src/shell/NewChatDialog.tsx` near `deriveHomeDir` (around lines 750-768)
- Test: `web/src/shell/NewChatDialog.test.tsx`

- [ ] **Step 1: Write failing pure-helper tests**

Add table-driven tests for the helper behavior:

```ts
it.each([
  ["/home/claude", "bacnv", "/home/claude/bacnv"],
  ["/root", "bacnv", "/root/bacnv"],
  ["/", "bacnv", "/bacnv"],
])("builds the default workspace", (home, username, expected) => {
  expect(defaultUserWorkspace(home, username)).toBe(expected);
});

it.each(["", ".", "..", "../escape", "/absolute", "a/b", "a\\b", " alice "])(
  "rejects unsafe username %s",
  (username) => expect(defaultUserWorkspace("/home/claude", username)).toBeNull(),
);
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
cd web && npx vitest run src/shell/NewChatDialog.test.tsx -t "default workspace|unsafe username"
```

Expected: FAIL because `defaultUserWorkspace` is not exported/implemented.

- [ ] **Step 3: Implement the minimal pure helper**

Use the existing normalized identity contract and return `null` for blank, non-single-segment, or traversal-capable values:

```ts
export function defaultUserWorkspace(home: string, username: string): string | null {
  const base = home.replace(/\/+$/, "") || "/";
  const user = username.trim();
  if (!/^[a-zA-Z0-9_-]+$/.test(user)) return null;
  return `${base}/${user}`;
}
```

If the repository's actual account username contract is lowercase plus `._-`, use that stricter existing regex rather than widening it; do not normalize a value silently.

- [ ] **Step 4: Run focused tests**

Run:

```bash
cd web && npx vitest run src/shell/NewChatDialog.test.tsx -t "default workspace|unsafe username"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/NewChatDialog.tsx web/src/shell/NewChatDialog.test.tsx
git commit -m "test: cover default user workspace paths"
```

## Task 2: Seed Claude native, Sonnet, high, and the default workspace

**Files:**
- Modify: `web/src/shell/NewChatDialog.tsx` around state initialization (lines 1748-1868) and workspace seeding (lines 2118-2130)
- Modify: `web/src/lib/identity.ts` only if identity readiness cannot be consumed from existing app initialization
- Test: `web/src/shell/NewChatDialog.flow.test.tsx` and/or `web/src/shell/NewChatDialog.test.tsx`

**Interfaces:**
- Consumes `getCurrentUserId(): string | null`, `derivedHome`, `recent`, `prefillSettled`, and the existing available-agent list.
- Produces New Chat state with Claude native selected when no valid persisted/project agent wins, `pickedModel === "sonnet"`, and `pickedEffort === "high"` for Claude native when no explicit stored choice exists.

- [ ] **Step 1: Add failing tests for defaults and precedence**

Cover:

```ts
it("defaults a fresh external session to Claude native, Sonnet, and high effort", async () => {
  // Render with an authenticated user bacnv and a host whose home is /home/claude.
  // Assert the picker/request state exposes claude-native, sonnet, and high.
});

it("seeds /home/claude/bacnv only after host home and identity resolve", async () => {
  // Resolve the home listing and identity, then assert the workspace field is the derived path.
});

it("does not overwrite a project, recent, or manually entered workspace", async () => {
  // Assert each higher-priority workspace remains unchanged after the default effect runs.
});
```

Use the existing test mocks and test IDs; do not introduce a second identity or filesystem client mock.

- [ ] **Step 2: Run focused tests and verify they fail**

```bash
cd web && npx vitest run src/shell/NewChatDialog.flow.test.tsx src/shell/NewChatDialog.test.tsx -t "Claude native|Sonnet|high|bacnv|overwrite"
```

Expected: FAIL because the current default is persisted/agent-list driven and model/effort are unset unless previously remembered.

- [ ] **Step 3: Implement default agent and options with precedence**

- Identify the existing available-agent ID/capability for Claude Code native (`claude-native-ui` or the registry's canonical ID); do not hardcode a display label if the list exposes a canonical ID.
- Change only the fallback branch used when there is no valid project/persisted/manual agent selection.
- In the existing native-harness seed effect, use `sonnet` and `high` only when there is no valid stored per-harness option and no landing draft value. Preserve remembered values and user edits.
- Ensure the defaults apply only to Claude native; Codex/Cursor/OpenCode and managed sandbox behavior remain unchanged.

- [ ] **Step 4: Extend the workspace seed effect**

Compute `defaultUserWorkspace(derivedHome, getCurrentUserId() ?? "")` and make it the final fallback after `recent[0]` and project prefill. Guard it with `selectedHostId !== null`, `!sandboxSelected`, `prefillSettled`, and an empty current workspace. Keep the existing once-per-host guard so late identity/home responses cannot overwrite explicit input.

If identity is not resolved when the effect first runs, trigger/use the existing app-level identity resolution and re-run through a minimal React state/query seam; never derive the username from the OS home path.

- [ ] **Step 5: Run focused tests**

```bash
cd web && npx vitest run src/shell/NewChatDialog.flow.test.tsx src/shell/NewChatDialog.test.tsx -t "Claude native|Sonnet|high|bacnv|overwrite"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/src/shell/NewChatDialog.tsx web/src/lib/identity.ts web/src/shell/NewChatDialog.flow.test.tsx web/src/shell/NewChatDialog.test.tsx
git commit -m "feat: default new sessions to user workspace"
```

## Task 3: Create the default directory before session creation

**Files:**
- Modify: `web/src/shell/NewChatDialog.tsx` in `handleCreate` around lines 2794-2890
- Modify: `web/src/hooks/useHostFilesystem.ts` only if the existing `createHostDirectory` export is not already accessible
- Test: `web/src/shell/NewChatDialog.flow.test.tsx`

**Interfaces:**
- Consumes the selected external `hostId`, `workspaceTrimmed`, and a ref/flag identifying that the workspace came from the default-user-workspace seed.
- Produces a directory-create request before the existing session-create request; directory-create failure prevents session creation.

- [ ] **Step 1: Add failing request-order/error tests**

```ts
it("creates a missing default directory before creating the session", async () => {
  // Mock POST /v1/hosts/{id}/directories and POST /v1/sessions.
  // Submit and assert directory POST precedes session POST with /home/claude/bacnv.
});

it("does not create a directory for an explicit workspace", async () => {
  // Submit with /home/claude/project and assert no directory-create request.
});

it("does not create a session when default directory creation fails", async () => {
  // Reject the directory request; assert session POST was not called and error is visible.
});
```

- [ ] **Step 2: Run focused tests and verify they fail**

```bash
cd web && npx vitest run src/shell/NewChatDialog.flow.test.tsx -t "directory|session"
```

Expected: FAIL because `handleCreate` currently submits without ensuring the default directory.

- [ ] **Step 3: Implement the minimal pre-submit ensure**

Import `createHostDirectory` from `useHostFilesystem.ts`. Track whether the current path was auto-seeded by this feature (a ref is sufficient). In `handleCreate`, before either normal or bundled external-host creation:

```ts
if (!sandboxSelected && selectedHostId && workspaceWasDefaultedRef.current) {
  await createHostDirectory(selectedHostId, workspaceTrimmed);
}
```

Treat the existing API's already-exists response as success only if the API exposes a distinct conflict status; otherwise use the existing host directory semantics and avoid swallowing permission/time-out errors. Keep managed sandbox out of this branch.

- [ ] **Step 4: Run focused tests**

```bash
cd web && npx vitest run src/shell/NewChatDialog.flow.test.tsx -t "directory|session"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/NewChatDialog.tsx web/src/hooks/useHostFilesystem.ts web/src/shell/NewChatDialog.flow.test.tsx
git commit -m "feat: ensure default workspace exists"
```

## Task 4: Full verification and cleanup

**Files:**
- Modify: only files needed to fix test/lint/type failures found by verification.
- Test: all affected web tests.

- [ ] **Step 1: Run affected test files**

```bash
cd web && npx vitest run src/shell/NewChatDialog.test.tsx src/shell/NewChatDialog.flow.test.tsx src/hooks/useHostFilesystem.test.ts
```

Expected: PASS.

- [ ] **Step 2: Run web typecheck/lint/build checks**

```bash
cd web && npm run typecheck
cd web && npm run lint
cd web && npm run build
```

Expected: all commands exit 0. If a script name differs, use the corresponding script listed by `npm run` and record the exact command/output.

- [ ] **Step 3: Run repository checks**

```bash
just lint
```

Expected: PASS. If `pre-commit` is unavailable in the environment, report that limitation rather than claiming success.

- [ ] **Step 4: Inspect the final diff**

```bash
git diff main...HEAD --stat
git diff main...HEAD --check
git status --short
```

Expected: only the approved spec, plan, and implementation/test files are changed; no generated artifacts or unrelated refactors.

- [ ] **Step 5: Commit any verification fixes**

```bash
git add <only-fixed-files>
git commit -m "test: verify default user workspace flow"
```

## Coverage checklist

- [ ] `/home/claude/bacnv` and `/root/bacnv` path joining covered.
- [ ] Unsafe username rejection covered.
- [ ] Claude Code native fallback covered.
- [ ] `sonnet` fallback covered.
- [ ] `high` effort fallback covered.
- [ ] Missing default directory creation covered.
- [ ] Existing-directory/race behavior covered.
- [ ] Explicit/project/recent workspace precedence covered.
- [ ] Directory failure blocks session creation.
- [ ] Managed sandbox and existing sessions remain unchanged.
