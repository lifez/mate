# Herdr presentation workspaces for Mate

## Goal

When Mate dispatches a task without `same_tab_as`, put the worker in its own Herdr
workspace instead of adding another tab to the supervisor workspace. The workspace is
the worktree container; its tab bar remains available for extra shells, servers, logs,
or other work in that same worktree.

Target shape:

```text
Spaces                         selected workspace tabs
Mate                           [supervisor]
└ migrate-admin-yearly-reviews [worker] [server] [logs]
└ migrate-admin-trade-commission
└ migrate-admin-portfolio-updater
```

This is a visual projection like Firstmate, not a native Herdr Git-worktree group.
The rows will not be collapsible children unless Herdr later supports grouping arbitrary
workspaces. Treehouse remains the owner of Git worktrees; Herdr only opens a terminal
workspace whose CWD is the leased path, so the task repository may differ from Mate's.

## Scope

- Add an opt-in `worker.workspace_per_task` boolean to `mate.config.json`.
  Default `false` preserves today's tab behavior.
- When enabled, `mate_dispatch` without `same_tab_as` creates a Herdr workspace with:
  - CWD = the exact Treehouse-leased worktree
  - label = `└ <task-id>`
  - `--no-focus`
- Save the returned workspace, worker tab, pane, and terminal identities before launching Pi.
- Keep Herdr's tab bar enabled. Users may add tabs manually inside the task workspace;
  Mate owns only the exact worker tab it created.
- Keep `same_tab_as` unchanged; an explicit shared-tab request wins over the config.
- Continuation reuses the saved pane/workspace and never creates another workspace.
- Existing completion confirmation closes only the exact worker tab. Herdr removes the
  workspace only when no other tabs remain; user-created tabs keep it alive.
- Treehouse lease return remains separate and must refuse while another tab/process still
  uses the leased worktree, preventing a live workspace from being reset underneath it.
- Existing tasks without the new field retain their current behavior. No data migration.
- A second Mate task still gets an independent Treehouse lease/worktree. Sharing one Git
  checkout between concurrent agents is out of scope because their edits and lifecycle
  ownership would collide. Use `mate_extend` for sequential work in the same checkout.

## Implementation sequence

### 0. Safe starting point

Do this only after current workers stop:

1. Confirm Mate has no `acquiring`, `launching`, or `running` tasks.
2. Back up the stopped `MATE_HOME`.
3. Start a fresh `MATE_MODE=dev pi` session in `/Users/win/mine/mate`.
4. Record the inspected Firstmate commit and relevant Herdr presentation source blobs in
   `UPSTREAM.md` / `UPSTREAM.json`; treat them as reference-only unless code is copied.

### 1. Red tests

Add focused tests before runtime changes:

- Default config still calls `herdr tab create` in the supervisor workspace.
- `workspace_per_task: true` calls `herdr workspace create --cwd <leased-path>
  --label "└ <task-id>" --no-focus`.
- The returned root pane identities are persisted and checked before `pane run`.
- A lost or malformed create receipt leaves the task in `attention`; duplicate dispatch
  does not create another workspace.
- `same_tab_as` still uses `pane split` and never creates a workspace.
- Continuation reuses the original presentation workspace.
- Closing Mate's worker tab preserves any additional user-created tabs/workspace.
- Lease return refuses while those tabs still have processes in the worktree.
- Existing task records without the new setting remain compatible.

### 2. Minimal runtime change

In `bin/mate.py`:

1. Validate `worker.workspace_per_task` as a strict boolean.
2. Save the resolved placement choice on initial dispatch before acquisition.
3. Preserve the launcher workspace separately from the worker endpoint workspace.
4. After Treehouse acquisition/startup:
   - shared-tab task: keep the existing `pane split` path;
   - workspace-per-task: call `workspace create` and use its seeded tab/root pane;
   - default: keep the existing `tab create` path.
5. Reuse existing endpoint, terminal, idle-shell, continuation, and fail-closed checks.

Do not add automatic adoption, retry, workspace lookup by label, or a second cleanup
system. Exact response IDs and the existing SQLite task journal remain authoritative.

### 3. Disposable real integration check

Extend `tests/live-smoke.py` using its private named Herdr server and disposable
Treehouse repository:

1. Create the supervisor workspace.
2. Dispatch one task from a repository/worktree path different from Mate's repository.
3. Assert the supervisor remains focused and gains no extra tab.
4. Assert one new workspace has label `└ <task-id>`, correct CWD, and the recorded IDs.
5. Continue the task and assert no additional workspace is created.
6. Add a second tab in the task workspace, complete the task, and close Mate's exact
   worker tab; assert the extra tab and workspace remain.
7. Assert lease return refuses while the extra tab still uses the worktree.
8. Close the fixture's extra tab, then return the lease through the existing separately
   confirmed path; assert the owner workspace, task branch, and report remain.

### 4. Ordering experiment only if needed

First try Herdr's native workspace insertion order. If task rows do not remain adjacent
to `Mate`, then evaluate a small protocol-19 `workspace.move` helper using the existing
Unix-socket transport pattern in `bin/herdr-eventwait.py`.

Ordering must be best-effort presentation only:

- verify session/socket, parent workspace, created workspace, focus, and full order;
- preserve relative order of every unrelated workspace;
- never fail or clean up a successfully launched worker only because ordering failed;
- never use labels as identity.

Do not import Firstmate's presentation token, recovery, stale-space sweeper, multi-home
ordering, or pre-0.8.0 focus workarounds unless a reproduced Mate failure requires them.
The installed Herdr is currently 0.8.0 / protocol 19.

### 5. Documentation and manual UAT

Update `README.md`, `CONFIGURATION.md`, and `mate.config.example.json` with the opt-in
setting and the fact that this is visual grouping, not Git-worktree ownership.

Manual acceptance:

- Enable `workspace_per_task`, reload Mate with workers stopped, and dispatch one small
  read-only task.
- The task appears in Spaces and does not add a top tab under `Mate`.
- Switching to it shows the native Pi TUI in the leased worktree and a normal tab bar.
- Creating `server` and `logs` tabs inside it keeps those tabs in the same workspace/CWD.
- A second Mate task from another repository gets its own row and isolated worktree.
- `same_tab_as: "supervisor"` still creates a pane in Mate's tab.
- Continuation stays in the same row.
- Completion never closes Mate or returns the lease without the existing confirmations.

## Verification

```sh
python3 -m unittest discover -s tests -v
node tests/extension-check.mjs
python3 tests/tui-smoke.py
python3 tests/live-smoke.py
```

The final evidence must include the unit red/green result and the real disposable Herdr
smoke. A screenshot alone is not sufficient.

## Rollback

Set `worker.workspace_per_task` to `false` and reload with workers stopped. This affects
new dispatches only. Existing presentation-workspace tasks continue and complete through
their saved exact endpoint identities; do not rewrite their records or move live panes.
User-created tabs are never part of rollback or automatic cleanup.
